"""Trading data endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select

from bobrito.api.deps import get_bot, verify_token
from bobrito.engine.bot import TradingBot
from bobrito.persistence.database import get_db
from bobrito.persistence.models import Position

router = APIRouter(prefix="/trading", tags=["Trading"], dependencies=[Depends(verify_token)])


@router.get("/balances")
async def get_balances(bot: TradingBot = Depends(get_bot)):
    """Return current broker balances."""
    try:
        broker = bot._broker
        if broker is None:
            raise HTTPException(status_code=503, detail="Broker not initialised")
        balances = await broker.get_balances()
        return {"balances": balances}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/positions")
async def get_positions(db=Depends(get_db)):
    """Return open positions."""
    result = await db.execute(
        select(Position).where(Position.status == "OPEN").order_by(desc(Position.opened_at))
    )
    positions = result.scalars().all()
    return {
        "positions": [
            {
                "id": p.id,
                "symbol": p.symbol,
                "side": p.side,
                "entry_price": p.entry_price,
                "quantity": p.quantity,
                "stop_price": p.stop_price,
                "target_price": p.target_price,
                "total_fees": p.total_fees,
                "opened_at": p.opened_at.isoformat() if p.opened_at else None,
            }
            for p in positions
        ]
    }


@router.get("/trades")
async def get_trades(limit: int = 50, db=Depends(get_db)):
    """Return recent closed positions (completed trades)."""
    result = await db.execute(
        select(Position)
        .where(Position.status == "CLOSED")
        .order_by(desc(Position.closed_at))
        .limit(limit)
    )
    trades = result.scalars().all()
    return {
        "trades": [
            {
                "id": t.id,
                "symbol": t.symbol,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "quantity": t.quantity,
                "realized_pnl": t.realized_pnl,
                "net_pnl": t.net_pnl,
                "total_fees": t.total_fees,
                "exit_reason": t.exit_reason,
                "opened_at": t.opened_at.isoformat() if t.opened_at else None,
                "closed_at": t.closed_at.isoformat() if t.closed_at else None,
            }
            for t in trades
        ]
    }


@router.get("/metrics")
async def get_metrics(bot: TradingBot = Depends(get_bot)):
    """Return portfolio and risk metrics."""
    return {
        "portfolio": bot.get_portfolio().stats(),
        "risk": bot.get_risk().state_dict(),
        "uptime_seconds": round(bot.uptime_seconds, 1),
    }
