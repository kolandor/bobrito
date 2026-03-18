"""UI data aggregation — transforms domain state into view models.

All data is sourced from the existing service layer. No domain logic is
duplicated here; this module only formats and aggregates for display.
"""

from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy import and_, desc, or_, select

from bobrito.config.settings import Settings
from bobrito.engine.bot import BotStatus, TradingBot
from bobrito.risk.manager import RiskManager
from bobrito.monitoring.logger import get_logger
from bobrito.persistence.database import DatabaseManager
from bobrito.persistence.models import ErrorLog, Position, PositionStatus, Signal, SystemEvent
from bobrito.ui.viewmodels import (
    BalancesVM,
    BotStatusVM,
    EventVM,
    MetricsVM,
    PositionVM,
    RiskBlockVM,
    RiskStatusVM,
    SignalVM,
    SituationVM,
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


def _blocks_from_risk(risk: "RiskManager") -> "list[RiskBlockVM]":
    """Convert raw block dicts from RiskManager into RiskBlockVM instances."""
    return [
        RiskBlockVM(
            type=b["type"],
            name=b["name"],
            reason=b["reason"],
            reset_tip=b["reset_tip"],
            severity=b["severity"],
            remaining_seconds=b.get("remaining_seconds", 0),
        )
        for b in risk.check_entry_blocks()
    ]

def _compute_ema_series(closes: list[float], period: int) -> list[float | None]:
    """Return EMA series aligned with *closes* (None during warmup period)."""
    if not closes or period <= 0:
        return []
    result: list[float | None] = [None] * min(period - 1, len(closes))
    if len(closes) < period:
        return result
    sma = sum(closes[:period]) / period
    result.append(round(sma, 2))
    k = 2.0 / (period + 1)
    for price in closes[period:]:
        prev = result[-1]
        result.append(round(prev * (1 - k) + price * k, 2))  # type: ignore[operator]
    return result


def _signal_friendly(
    signal_type: str,
    raw: str | None,
    stop_price: float | None,
    target_price: float | None,
) -> str:
    """Convert a raw strategy explanation string into a user-friendly sentence."""
    raw = raw or ""
    r = raw.lower()

    if signal_type == "BUY":
        parts = ["All entry conditions met: 5-min uptrend confirmed, 1-min pullback detected, momentum resuming, volume above average."]
        if stop_price and target_price:
            rr = (target_price - stop_price) / max(stop_price, 1) * 100
            parts.append(f"Stop: ${stop_price:,.2f} | Target: ${target_price:,.2f} | R/R ≈ {rr:.1f}%")
        return " ".join(parts)

    if signal_type == "EXIT":
        if "momentum failure" in r or "close < fast ema" in r:
            return (
                "Exit signal — upward momentum has failed: the 1-min close dropped "
                "below the fast EMA, indicating the pullback-resumption pattern broke down. "
                "Open position should be closed to protect capital."
            )
        return "Exit signal — strategy conditions no longer met for holding the position."

    # ── HOLD ──────────────────────────────────────────────────────────────
    if "insufficient" in r or "history" in r:
        return "Waiting for candle history — the buffer needs at least 30 one-minute and 25 five-minute closed candles before the strategy can evaluate signals."
    if "not ready" in r:
        return "Indicators warming up — not enough candle data yet for EMA and ATR calculations."
    if "sideways" in r:
        return (
            "Market is sideways — the fast EMA is not meaningfully above the slow EMA on the 5-min chart. "
            "The strategy will not open positions until a clear uptrend is established."
        )
    if "momentum intact" in r or "position held" in r:
        return "Position is healthy — the 1-min close is still above the fast EMA. Momentum intact; continuing to hold."

    # Parse "HOLD: reason1, reason2" format
    missing = []
    if "no_uptrend_5m" in raw:
        missing.append("5-min uptrend not confirmed (fast EMA ≤ slow EMA)")
    if "no_pullback" in raw:
        missing.append("no pullback toward the slow EMA detected on 1-min chart")
    if "no_resumption" in raw:
        missing.append("momentum resumption not yet confirmed (price has not crossed back above fast EMA)")
    if "low_volume" in raw:
        missing.append("volume is below the required threshold (current bar volume too low)")
    if missing:
        return "Holding — entry conditions not yet met: " + "; ".join(missing) + "."

    return raw if raw else "No signal data."


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
        tp_fmt = f"${target_price:,.2f}" if target_price else None

        # The live price reached the target (trigger), but the fill is a
        # market SELL which executes with slippage — so fill < target is normal.
        slippage_note = ""
        if exit_price and target_price and exit_price < target_price:
            slippage_note = (
                f" The market sell order filled at {xp} (below the target because "
                f"paper-trading simulates 5 bps of market slippage on every SELL)."
            )

        trigger_line = (
            f"Take-profit triggered — the live price reached the target level "
            f"{tp_fmt or 'the target'}.{slippage_note} "
        )

        # Explain fee-dominated loss if gross PnL was positive but net is negative
        fee_note = ""
        if (
            exit_price
            and entry_price
            and net_pnl is not None
            and net_pnl < 0
        ):
            gross = (exit_price - entry_price) * quantity
            if gross > 0:
                fees = gross - net_pnl   # net = gross - fees → fees = gross - net
                fee_note = (
                    f"Despite a positive gross price move "
                    f"(+${gross:,.4f} USDT), the net result is a loss of "
                    f"${abs(net_pnl):,.4f} USDT because trading fees "
                    f"(~${fees:,.4f} USDT, ~0.1 %% per side) exceeded the "
                    f"gross profit. The position size ({qty_str} BTC, "
                    f"~${entry_price * quantity:,.2f} notional) was too small "
                    f"for this price move to be profitable after fees."
                )

        entry_line = f"Entry was {ep} for {qty_str} BTC. "
        if fee_note:
            return trigger_line + entry_line + fee_note
        return trigger_line + entry_line + f"Position closed with a {pnl_str}."

    if exit_reason == "STOP_LOSS":
        sl = f"${stop_price:,.2f}" if stop_price else "the stop level"
        # Fill can be slightly below stop due to slippage (gapping / market order)
        slippage_note = ""
        if exit_price and stop_price and exit_price < stop_price:
            slippage_note = (
                f" The market sell filled at {xp} (below the stop level "
                f"due to simulated slippage)."
            )
        return (
            f"Stop-loss triggered — price fell to {xp}, breaching the stop at {sl}.{slippage_note} "
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
        blocks = _blocks_from_risk(self._bot.get_risk())
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
            active_blocks=blocks,
            is_trade_blocked=len(blocks) > 0,
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
        risk = self._bot.get_risk()
        s = risk.state_dict()
        blocks = _blocks_from_risk(risk)
        cooldown_active = any(b.type == "cooldown" for b in blocks)
        lim = s["limits"]
        dfl = s["defaults"]
        return RiskStatusVM(
            safe_mode=s["safe_mode"],
            daily_trades=s["daily_trades"],
            daily_pnl=s["daily_pnl"],
            consecutive_losses=s["consecutive_losses"],
            current_day=s["current_day"],
            max_daily_loss_pct=lim["max_daily_loss_pct"],
            max_consecutive_losses=lim["max_consecutive_losses"],
            max_trades_per_day=lim["max_trades_per_day"],
            min_free_balance_usdt=lim["min_free_balance_usdt"],
            initial_capital_usdt=self._settings.initial_capital_usdt,
            default_max_daily_loss_pct=dfl["max_daily_loss_pct"],
            default_max_consecutive_losses=dfl["max_consecutive_losses"],
            default_max_trades_per_day=dfl["max_trades_per_day"],
            default_min_free_balance_usdt=dfl["min_free_balance_usdt"],
            has_overrides=s["has_overrides"],
            cooldown_active=cooldown_active,
            active_blocks=blocks,
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

    async def get_situation(self, db: DatabaseManager) -> SituationVM:
        """Aggregate data for the Current Situation dashboard block."""
        # ── Recent signals from DB ────────────────────────────────────────
        signals: list[SignalVM] = []
        try:
            async with db.session() as sess:
                result = await sess.execute(
                    select(Signal).order_by(desc(Signal.created_at)).limit(5)
                )
                for row in result.scalars().all():
                    st = row.signal_type.value if hasattr(row.signal_type, "value") else row.signal_type
                    signals.append(
                        SignalVM(
                            signal_type=st,
                            price=row.price,
                            timestamp=row.created_at.strftime("%H:%M:%S"),
                            explanation_friendly=_signal_friendly(st, row.explanation, row.stop_price, row.target_price),
                            ema_fast=row.ema_fast,
                            ema_slow=row.ema_slow,
                            stop_price=row.stop_price,
                            target_price=row.target_price,
                            acted_on=bool(row.acted_on),
                            rejected_reason=getattr(row, "rejected_reason", None),
                        )
                    )
        except Exception as exc:
            log.debug(f"get_situation signals error: {exc}")

        # ── Candle chart data from in-memory buffer ───────────────────────
        chart_labels: list[str] = []
        chart_closes: list[float] = []
        chart_ema_fast: list[float | None] = []
        chart_ema_slow: list[float | None] = []
        chart_open_markers: list[float | None] = []
        chart_close_profit_markers: list[float | None] = []
        chart_close_loss_markers: list[float | None] = []
        current_price: float | None = None
        buffer_ready = False
        chart_start: datetime | None = None
        chart_end: datetime | None = None

        try:
            candles = self._bot._buf1.latest(80)
            if candles:
                closes = [c.close for c in candles]
                chart_labels = [c.open_time.strftime("%H:%M") for c in candles]
                chart_closes = closes
                fp = self._settings.ema_fast
                sp = self._settings.ema_slow
                chart_ema_fast = _compute_ema_series(closes, fp)
                chart_ema_slow = _compute_ema_series(closes, sp)
                current_price = closes[-1]
                buffer_ready = len(candles) >= 30
                chart_start = candles[0].open_time
                chart_end = candles[-1].open_time
                n = len(candles)
                chart_open_markers = [None] * n
                chart_close_profit_markers = [None] * n
                chart_close_loss_markers = [None] * n
        except Exception as exc:
            log.debug(f"get_situation candles error: {exc}")

        snap = self._bot.get_last_snapshot()
        if snap:
            current_price = snap.last_price

        # ── Position open/close markers ───────────────────────────────────
        if chart_labels and chart_start is not None and chart_end is not None:
            try:
                # Build a label→index map for O(1) lookup (last occurrence wins
                # when two candles share the same HH:MM label, e.g. across hours)
                label_index: dict[str, int] = {
                    lbl: i for i, lbl in enumerate(chart_labels)
                }
                # Generous time window: anything opened OR closed within the range
                async with db.session() as sess:
                    result = await sess.execute(
                        select(Position).where(
                            or_(
                                and_(
                                    Position.opened_at >= chart_start,
                                    Position.opened_at <= chart_end,
                                ),
                                and_(
                                    Position.closed_at >= chart_start,
                                    Position.closed_at <= chart_end,
                                ),
                            )
                        )
                    )
                    for pos in result.scalars().all():
                        # ── Entry marker ──────────────────────────────────
                        if pos.opened_at:
                            lbl = pos.opened_at.strftime("%H:%M")
                            if lbl in label_index:
                                chart_open_markers[label_index[lbl]] = pos.entry_price

                        # ── Exit marker (only for closed positions) ───────
                        if pos.closed_at and pos.exit_price is not None and pos.status == PositionStatus.CLOSED:
                            lbl = pos.closed_at.strftime("%H:%M")
                            if lbl in label_index:
                                idx = label_index[lbl]
                                pnl = pos.net_pnl or 0.0
                                if pnl >= 0:
                                    chart_close_profit_markers[idx] = pos.exit_price
                                else:
                                    chart_close_loss_markers[idx] = pos.exit_price
            except Exception as exc:
                log.debug(f"get_situation position markers error: {exc}")

        return SituationVM(
            signals=signals,
            symbol=self._bot.get_status_dict()["symbol"],
            current_price=current_price,
            buffer_ready=buffer_ready,
            ema_fast_period=self._settings.ema_fast,
            ema_slow_period=self._settings.ema_slow,
            chart_labels=chart_labels,
            chart_closes=chart_closes,
            chart_ema_fast=chart_ema_fast,
            chart_ema_slow=chart_ema_slow,
            chart_open_markers=chart_open_markers,
            chart_close_profit_markers=chart_close_profit_markers,
            chart_close_loss_markers=chart_close_loss_markers,
        )

    async def get_signals_batch(
        self,
        db: DatabaseManager,
        offset: int = 0,
        limit: int = 30,
        signal_type: str | None = None,
    ) -> tuple[list[SignalVM], bool]:
        """Return (signals, has_more) for the Signals page with pagination."""
        try:
            async with db.session() as sess:
                query = select(Signal).order_by(desc(Signal.created_at))
                if signal_type and signal_type.upper() != "ALL":
                    query = query.where(Signal.signal_type == signal_type.upper())
                # Fetch one extra row to know whether another page exists
                query = query.offset(offset).limit(limit + 1)
                result = await sess.execute(query)
                rows = result.scalars().all()
                has_more = len(rows) > limit
                rows = rows[:limit]
                signals: list[SignalVM] = []
                for r in rows:
                    st = r.signal_type.value if hasattr(r.signal_type, "value") else r.signal_type
                    signals.append(
                        SignalVM(
                            id=r.id,
                            signal_type=st,
                            price=r.price,
                            timestamp=r.created_at.strftime("%H:%M:%S"),
                            created_at_full=r.created_at.strftime("%Y-%m-%d %H:%M"),
                            explanation_friendly=_signal_friendly(st, r.explanation, r.stop_price, r.target_price),
                            ema_fast=r.ema_fast,
                            ema_slow=r.ema_slow,
                            atr=r.atr,
                            stop_price=r.stop_price,
                            target_price=r.target_price,
                            acted_on=bool(r.acted_on),
                            rejected_reason=getattr(r, "rejected_reason", None),
                            regime=r.regime.value if hasattr(r.regime, "value") else r.regime,
                        )
                    )
                return signals, has_more
        except Exception as exc:
            log.debug(f"get_signals_batch error: {exc}")
            return [], False

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
