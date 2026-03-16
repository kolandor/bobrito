"""Risk limit management endpoints.

All endpoints require a valid Bearer token (same as other API routes).

Endpoints:
  GET  /risk/limits          — current effective limits + defaults
  POST /risk/reset-cooldown  — clear active cooldown & consecutive loss streak
  PATCH /risk/limits         — override one or more limit parameters
  POST /risk/restore-defaults — revert all overrides to ENV-file values
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from bobrito.api.deps import get_bot, verify_token
from bobrito.engine.bot import TradingBot

router = APIRouter(
    prefix="/risk",
    tags=["Risk"],
    dependencies=[Depends(verify_token)],
)


class LimitsUpdateRequest(BaseModel):
    max_consecutive_losses: int | None = Field(
        None,
        ge=1,
        description="Maximum number of consecutive losing trades allowed.",
    )
    max_daily_loss_pct: float | None = Field(
        None,
        ge=0.1,
        le=20.0,
        description="Maximum daily loss as a percentage of initial capital.",
    )
    min_free_balance_usdt: float | None = Field(
        None,
        ge=0.0,
        description="Minimum free USDT balance that must remain untouched.",
    )
    max_trades_per_day: int | None = Field(
        None,
        ge=1,
        description="Maximum number of trades allowed per trading day.",
    )


@router.get("/limits")
async def get_limits(bot: TradingBot = Depends(get_bot)):
    """Return current effective risk limits, their ENV defaults, and runtime state."""
    risk = bot.get_risk()
    return {
        **risk.state_dict(),
        "cooldown_active": risk._last_loss_time is not None,
    }


@router.post("/reset-cooldown")
async def reset_cooldown(bot: TradingBot = Depends(get_bot)):
    """Reset the active post-loss cooldown timer and consecutive loss streak.

    This is a conscious operator override — use when you have reviewed the
    situation and want to allow new entries before the cooldown expires.
    """
    try:
        await bot.get_risk().reset_cooldown()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {
        "message": "Post-loss cooldown and consecutive loss streak reset successfully.",
        "limits": bot.get_risk().limits_dict(),
    }


@router.patch("/limits")
async def update_limits(
    body: LimitsUpdateRequest,
    bot: TradingBot = Depends(get_bot),
):
    """Override one or more risk limit parameters.

    Only the provided fields are changed; omitted fields retain their
    current values. Overrides revert to ENV defaults automatically at midnight.
    """
    risk = bot.get_risk()
    changed: list[str] = []

    if body.max_consecutive_losses is not None:
        risk.set_max_consecutive_losses(body.max_consecutive_losses)
        changed.append("max_consecutive_losses")
    if body.max_daily_loss_pct is not None:
        risk.set_max_daily_loss_pct(body.max_daily_loss_pct)
        changed.append("max_daily_loss_pct")
    if body.min_free_balance_usdt is not None:
        risk.set_min_free_balance_usdt(body.min_free_balance_usdt)
        changed.append("min_free_balance_usdt")
    if body.max_trades_per_day is not None:
        risk.set_max_trades_per_day(body.max_trades_per_day)
        changed.append("max_trades_per_day")

    if not changed:
        raise HTTPException(status_code=422, detail="No limit fields provided in request body.")

    return {
        "message": f"Updated: {', '.join(changed)}.",
        "limits": risk.limits_dict(),
    }


@router.post("/restore-defaults")
async def restore_defaults(bot: TradingBot = Depends(get_bot)):
    """Restore all risk limit overrides to their ENV-file values."""
    bot.get_risk().restore_defaults()
    return {
        "message": "All risk limit overrides cleared — reverted to ENV-file defaults.",
        "limits": bot.get_risk().limits_dict(),
    }
