"""Risk management control endpoints.

Provides manual override controls for all risk limiters:
  GET  /risk/params              — current effective values + override flags
  GET  /risk/state               — live counter state
  PATCH /risk/params             — override one or more limits
  POST /risk/params/restore      — revert all to .env defaults
  POST /risk/reset/cooldown      — clear post-loss cooldown timer
  POST /risk/reset/consecutive-losses — zero the loss streak
  POST /risk/reset/daily-counters     — zero today's trades + PnL
  POST /risk/reset/all                — all of the above at once
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from bobrito.api.deps import get_bot, verify_token
from bobrito.engine.bot import TradingBot

router = APIRouter(
    prefix="/risk",
    tags=["Risk Controls"],
    dependencies=[Depends(verify_token)],
)


def _get_risk(bot: TradingBot = Depends(get_bot)):
    if bot._risk is None:  # noqa: SLF001
        raise HTTPException(status_code=503, detail="Bot not initialised")
    return bot._risk  # noqa: SLF001


# ── Read ──────────────────────────────────────────────────────────────────────


@router.get("/params", summary="Get current effective risk parameters")
async def get_risk_params(risk=Depends(_get_risk)):
    """Return all 5 risk limits with their current (possibly overridden) value,
    the original .env default, and whether an override is active."""
    return risk.get_params()


@router.get("/state", summary="Get live risk counter state")
async def get_risk_state(risk=Depends(_get_risk)):
    """Return current in-memory counters: daily trades, PnL, consecutive losses."""
    return risk.state_dict()


# ── Patch parameters ──────────────────────────────────────────────────────────


class RiskParamsPatch(BaseModel):
    max_consecutive_losses: Annotated[int | None, Field(gt=0, le=100)] = None
    max_daily_loss_pct: Annotated[float | None, Field(gt=0.0, le=100.0)] = None
    cooldown_minutes_after_losses: Annotated[int | None, Field(ge=0, le=1440)] = None
    max_trades_per_day: Annotated[int | None, Field(gt=0, le=500)] = None
    min_free_balance_usdt: Annotated[float | None, Field(ge=0.0)] = None


@router.patch("/params", summary="Override one or more risk parameters")
async def patch_risk_params(body: RiskParamsPatch, risk=Depends(_get_risk)):
    """Override risk parameters at runtime. Only provided (non-null) fields are
    updated. Changes take effect on the very next trade evaluation.
    Overrides persist until the process restarts or ``/risk/params/restore``
    is called."""
    updated = risk.set_params(
        max_consecutive_losses=body.max_consecutive_losses,
        max_daily_loss_pct=body.max_daily_loss_pct,
        cooldown_minutes_after_losses=body.cooldown_minutes_after_losses,
        max_trades_per_day=body.max_trades_per_day,
        min_free_balance_usdt=body.min_free_balance_usdt,
    )
    return {"message": "Risk parameters updated", "params": updated}


@router.post("/params/restore", summary="Restore all parameters to .env defaults")
async def restore_risk_params(risk=Depends(_get_risk)):
    """Clear every runtime override and revert to the values in the .env file."""
    params = risk.restore_defaults()
    return {"message": "Risk parameters restored to .env defaults", "params": params}


# ── Counter resets ────────────────────────────────────────────────────────────


@router.post("/reset/cooldown", summary="Clear the post-loss cooldown timer")
async def reset_cooldown(risk=Depends(_get_risk)):
    """Cancel the active cooldown so the bot can accept new entries immediately."""
    risk.reset_cooldown()
    return {"message": "Cooldown timer cleared"}


@router.post(
    "/reset/consecutive-losses",
    summary="Reset the consecutive-loss streak to 0",
)
async def reset_consecutive_losses(risk=Depends(_get_risk)):
    """Manually reset the consecutive-loss counter. Use after reviewing the
    losing trades and confirming you are ready to resume."""
    risk.reset_consecutive_losses()
    return {"message": "Consecutive loss counter reset to 0"}


@router.post("/reset/daily-counters", summary="Reset today's trade count and PnL")
async def reset_daily_counters(risk=Depends(_get_risk)):
    """Zero the daily trade count and realised PnL, granting fresh daily
    headroom immediately without waiting for midnight."""
    risk.reset_daily_counters()
    return {"message": "Daily counters (trades + PnL) reset"}


@router.post("/reset/all", summary="Reset every limiter counter at once")
async def reset_all_counters(risk=Depends(_get_risk)):
    """One-shot reset: clears the cooldown timer, consecutive-loss streak,
    daily trade count, and daily PnL simultaneously."""
    risk.reset_all_counters()
    return {"message": "All risk counters reset"}
