"""Health and status endpoints."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends

from bobrito.api.deps import get_bot, verify_token
from bobrito.engine.bot import TradingBot

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health():
    """Liveness probe — always returns 200 if the process is up."""
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@router.get("/status", dependencies=[Depends(verify_token)])
async def status(bot: TradingBot = Depends(get_bot)):
    """Full bot runtime status."""
    return bot.get_status_dict()
