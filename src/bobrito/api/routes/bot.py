"""Bot control endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from bobrito.api.deps import get_bot, verify_token
from bobrito.engine.bot import BotStatus, TradingBot

router = APIRouter(prefix="/bot", tags=["Bot Control"], dependencies=[Depends(verify_token)])


@router.post("/start")
async def start(bot: TradingBot = Depends(get_bot)):
    if bot.status == BotStatus.RUNNING:
        return {"message": "Bot is already running", "status": bot.status.value}
    try:
        await bot.start()
        return {"message": "Bot started", "status": bot.status.value}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/stop")
async def stop(bot: TradingBot = Depends(get_bot)):
    if bot.status == BotStatus.STOPPED:
        return {"message": "Bot is already stopped", "status": bot.status.value}
    await bot.stop()
    return {"message": "Bot stopped", "status": bot.status.value}


@router.post("/pause")
async def pause(bot: TradingBot = Depends(get_bot)):
    if bot.status != BotStatus.RUNNING:
        raise HTTPException(status_code=400, detail="Bot must be running to pause")
    bot.pause()
    return {"message": "Bot paused", "status": bot.status.value}


@router.post("/resume")
async def resume(bot: TradingBot = Depends(get_bot)):
    if bot.status != BotStatus.PAUSED:
        raise HTTPException(status_code=400, detail="Bot must be paused to resume")
    bot.resume()
    return {"message": "Bot resumed", "status": bot.status.value}


@router.post("/emergency-stop")
async def emergency_stop(bot: TradingBot = Depends(get_bot)):
    await bot.emergency_stop()
    return {"message": "Emergency stop executed", "status": bot.status.value}
