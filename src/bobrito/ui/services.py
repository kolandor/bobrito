"""UI data aggregation — transforms domain state into view models.

All data is sourced from the existing service layer. No domain logic is
duplicated here; this module only formats and aggregates for display.
"""

from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy import desc, select

from bobrito.config.settings import Settings
from bobrito.engine.bot import BotStatus, TradingBot
from bobrito.monitoring.logger import get_logger
from bobrito.persistence.database import DatabaseManager
from bobrito.persistence.models import ErrorLog, Position, PositionStatus, SystemEvent
from bobrito.ui.viewmodels import (
    BalancesVM,
    BotStatusVM,
    EventVM,
    MetricsVM,
    PositionVM,
    RiskStatusVM,
    SystemStatusVM,
    TradeVM,
)

log = get_logger("ui.services")

_FEED_STALE_THRESHOLD_SECONDS = 60.0


def _format_uptime(seconds: float) -> str:
    if seconds <= 0:
        return "0s"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m {int(seconds % 60)}s"
    h = int(seconds / 3600)
    m = int((seconds % 3600) / 60)
    return f"{h}h {m}m"


def _format_lifetime(entry_time: datetime) -> str:
    return _format_uptime((datetime.utcnow() - entry_time).total_seconds())


def _format_feed_lag(lag: float | None) -> str | None:
    if lag is None:
        return None
    if lag > 60:
        return f"{lag:.0f}s ⚠"
    if lag > 10:
        return f"{lag:.1f}s"
    return f"{lag:.2f}s"


def _pnl_class(pnl: float | None) -> str:
    if pnl is None:
        return "text-slate-400"
    if pnl > 0:
        return "text-green-400"
    if pnl < 0:
        return "text-red-400"
    return "text-slate-400"


_STATUS_STYLES: dict[str, tuple[str, str]] = {
    "running": ("text-green-400", "bg-green-400 animate-pulse"),
    "paused": ("text-yellow-400", "bg-yellow-400"),
    "stopped": ("text-slate-400", "bg-slate-500"),
    "idle": ("text-slate-400", "bg-slate-500"),
    "error": ("text-red-400", "bg-red-500 animate-pulse"),
    "starting": ("text-blue-400", "bg-blue-400 animate-pulse"),
    "stopping": ("text-orange-400", "bg-orange-400"),
}

def _trade_explanation(
    exit_reason: str | None,
    entry_price: float,
    exit_price: float | None,
    stop_price: float | None,
    target_price: float | None,
    net_pnl: float | None,
    quantity: float,
) -> str:
    """Build a human-readable sentence explaining why a position was closed."""
    ep = f"${entry_price:,.2f}" if entry_price else "?"
    xp = f"${exit_price:,.2f}" if exit_price else "?"
    pnl_str = (
        f"{'gain' if (net_pnl or 0) >= 0 else 'loss'} "
        f"{'+'if (net_pnl or 0) >= 0 else ''}{net_pnl:.4f} USDT after fees"
        if net_pnl is not None
        else "PnL unknown"
    )
    qty_str = f"{quantity:.6f}"

    if exit_reason == "TAKE_PROFIT":
        tp = f"${target_price:,.2f}" if target_price else "the target"
        return (
            f"Take-profit hit: {xp} reached or exceeded the target level {tp}. "
            f"Entry was {ep} for {qty_str} BTC. "
            f"Position closed with a {pnl_str}."
        )
    if exit_reason == "STOP_LOSS":
        sl = f"${stop_price:,.2f}" if stop_price else "the stop level"
        return (
            f"Stop-loss triggered: price fell to {xp}, breaching the stop at {sl}. "
            f"Entry was {ep} for {qty_str} BTC. "
            f"Position closed with a {pnl_str}."
        )
    if exit_reason == "MOMENTUM_FAILURE":
        return (
            f"Strategy exit signal: the trend-pullback strategy detected that "
            f"upward momentum had reversed or entry conditions were no longer valid. "
            f"Position was closed at {xp} (entry {ep}, {qty_str} BTC). "
            f"Result: {pnl_str}."
        )
    if exit_reason == "EMERGENCY":
        return (
            f"Emergency stop executed: the bot was halted manually and all open "
            f"positions were force-closed at market price ({xp}). "
            f"Entry was {ep} for {qty_str} BTC. Result: {pnl_str}."
        )
    if exit_reason == "SESSION_CLOSE":
        return (
            f"Session boundary: position auto-closed at {xp} due to end-of-session "
            f"protocol. Entry was {ep} for {qty_str} BTC. Result: {pnl_str}."
        )
    if exit_reason == "MANUAL":
        return (
            f"Manual close: position closed by the operator at {xp}. "
            f"Entry was {ep} for {qty_str} BTC. Result: {pnl_str}."
        )
    return ""


_EVENT_CLASSES: dict[str, str] = {
    "BOT_START": "text-green-400",
    "BOT_STOP": "text-slate-400",
    "EMERGENCY_STOP": "text-red-400",
    "ERROR": "text-red-400",
}


class UIService:
    """Aggregates data from the bot and DB for UI templates."""

    def __init__(self, bot: TradingBot, settings: Settings) -> None:
        self._bot = bot
        self._settings = settings

    def get_bot_status(self) -> BotStatusVM:
        d = self._bot.get_status_dict()
        status = d["status"]
        color, dot = _STATUS_STYLES.get(status, ("text-slate-400", "bg-slate-500"))
        feed_lag = d.get("feed_lag_seconds")
        return BotStatusVM(
            status=status,
            mode=d["mode"],
            uptime_formatted=_format_uptime(d["uptime_seconds"]),
            symbol=d["symbol"],
            paused=d["paused"],
            safe_mode=d["safe_mode"],
            snapshot_count=d["snapshot_count"],
            feed_lag_seconds=feed_lag,
            feed_lag_formatted=_format_feed_lag(feed_lag),
            status_color=color,
            status_dot=dot,
            is_feed_stale=feed_lag is not None and feed_lag > _FEED_STALE_THRESHOLD_SECONDS,
        )

    async def get_balances(self) -> BalancesVM:
        try:
            broker = self._bot._broker
            if broker is None:
                return BalancesVM(0.0, 0.0, 0.0, 0.0, None, error="Broker not initialised")
            balances = await broker.get_balances()
            free_usdt = balances.get("USDT", 0.0)
            free_btc = balances.get("BTC", 0.0)
            snap = self._bot.get_last_snapshot()
            btc_price = snap.last_price if snap else None
            equity = free_usdt + free_btc * btc_price if btc_price is not None else None
            return BalancesVM(
                free_usdt=free_usdt,
                free_btc=free_btc,
                locked_usdt=0.0,
                locked_btc=0.0,
                equity_usdt=equity,
                btc_price=btc_price,
            )
        except Exception as exc:
            log.debug(f"get_balances error: {exc}")
            return BalancesVM(0.0, 0.0, 0.0, 0.0, None, error=str(exc))

    def get_position(self) -> PositionVM:
        pos = self._bot.get_portfolio().get_open_position()
        if pos is None:
            return PositionVM(
                has_position=False,
                symbol="",
                entry_price=0.0,
                quantity=0.0,
                stop_price=0.0,
                target_price=0.0,
                unrealised_pnl=0.0,
                side="",
                entry_time=None,
                lifetime_formatted="",
            )
        pnl = pos.unrealised_pnl
        return PositionVM(
            has_position=True,
            symbol=pos.symbol,
            entry_price=pos.entry_price,
            quantity=pos.quantity,
            stop_price=pos.stop_price,
            target_price=pos.target_price,
            unrealised_pnl=pnl,
            side=pos.side,
            entry_time=pos.entry_time,
            lifetime_formatted=_format_lifetime(pos.entry_time),
            pnl_class=_pnl_class(pnl),
        )

    def get_metrics(self) -> MetricsVM:
        stats = self._bot.get_portfolio().stats()
        risk = self._bot.get_risk()
        daily = risk.daily_pnl
        total = stats["total_pnl_usdt"]
        return MetricsVM(
            daily_pnl=daily,
            total_pnl=total,
            total_trades=stats["total_trades"],
            wins=stats["wins"],
            losses=stats["losses"],
            win_rate=stats["win_rate_pct"],
            max_drawdown=stats["max_drawdown_pct"],
            daily_pnl_class=_pnl_class(daily),
            total_pnl_class=_pnl_class(total),
        )

    def get_risk_status(self) -> RiskStatusVM:
        s = self._bot.get_risk().state_dict()
        return RiskStatusVM(
            safe_mode=s["safe_mode"],
            daily_trades=s["daily_trades"],
            daily_pnl=s["daily_pnl"],
            consecutive_losses=s["consecutive_losses"],
            current_day=s["current_day"],
            max_daily_loss_pct=self._settings.max_daily_loss_pct,
            max_consecutive_losses=self._settings.max_consecutive_losses,
            max_trades_per_day=self._settings.max_trades_per_day,
        )

    def get_system_status(self) -> SystemStatusVM:
        d = self._bot.get_status_dict()
        feed_lag = d.get("feed_lag_seconds")
        ws_connected = (
            self._bot.status == BotStatus.RUNNING and d.get("snapshot_count", 0) > 0
        )
        return SystemStatusVM(
            ws_connected=ws_connected,
            feed_lag_seconds=feed_lag,
            snapshot_count=d["snapshot_count"],
            feed_lag_formatted=_format_feed_lag(feed_lag),
            is_feed_stale=feed_lag is not None and feed_lag > _FEED_STALE_THRESHOLD_SECONDS,
        )

    async def get_recent_trades(self, db: DatabaseManager, limit: int = 20) -> list[TradeVM]:
        try:
            async with db.session() as sess:
                result = await sess.execute(
                    select(Position)
                    .where(Position.status == PositionStatus.CLOSED)
                    .order_by(desc(Position.closed_at))
                    .limit(limit)
                )
                rows = result.scalars().all()
                trades = []
                for r in rows:
                    pnl = r.net_pnl
                    reason = r.exit_reason.value if hasattr(r.exit_reason, "value") else r.exit_reason
                    trades.append(
                        TradeVM(
                            id=r.id,
                            symbol=r.symbol,
                            entry_price=r.entry_price,
                            exit_price=r.exit_price,
                            quantity=r.quantity,
                            net_pnl=pnl,
                            exit_reason=reason,
                            opened_at=r.opened_at.strftime("%Y-%m-%d %H:%M") if r.opened_at else "",
                            closed_at=r.closed_at.strftime("%Y-%m-%d %H:%M") if r.closed_at else "",
                            stop_price=r.stop_price,
                            target_price=r.target_price,
                            explanation=_trade_explanation(
                                reason,
                                r.entry_price,
                                r.exit_price,
                                r.stop_price,
                                r.target_price,
                                pnl,
                                r.quantity,
                            ),
                            pnl_class=_pnl_class(pnl),
                        )
                    )
                return trades
        except Exception as exc:
            log.debug(f"get_recent_trades error: {exc}")
            return []

    async def get_recent_events(self, db: DatabaseManager, limit: int = 30) -> list[EventVM]:
        try:
            async with db.session() as sess:
                result = await sess.execute(
                    select(SystemEvent)
                    .order_by(desc(SystemEvent.created_at))
                    .limit(limit)
                )
                rows = result.scalars().all()
                return [
                    EventVM(
                        id=r.id,
                        created_at=r.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                        event_type=r.event_type,
                        description=r.description,
                        mode=r.mode,
                        event_class=_EVENT_CLASSES.get(r.event_type, "text-slate-400"),
                    )
                    for r in rows
                ]
        except Exception as exc:
            log.debug(f"get_recent_events error: {exc}")
            return []

    async def get_error_logs(self, db: DatabaseManager, limit: int = 20) -> list[dict]:
        try:
            async with db.session() as sess:
                result = await sess.execute(
                    select(ErrorLog).order_by(desc(ErrorLog.created_at)).limit(limit)
                )
                return [
                    {
                        "id": r.id,
                        "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                        "module": r.module,
                        "error_type": r.error_type,
                        "message": r.message,
                        "mode": r.mode,
                    }
                    for r in result.scalars().all()
                ]
        except Exception as exc:
            log.debug(f"get_error_logs error: {exc}")
            return []
