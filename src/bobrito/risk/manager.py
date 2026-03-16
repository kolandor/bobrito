"""Risk Management Layer.

Rules enforced (all must pass for a trade to be allowed):
  1. No existing open position
  2. Daily loss limit not breached
  3. Consecutive loss limit not reached
  4. Cooldown period respected
  5. Max daily trades not reached
  6. Minimum free balance maintained
  7. Safe mode not active (set on critical errors)

Position sizing follows fixed-fractional risk:
    qty = (capital × risk_pct) / stop_distance
Applied with exchange step-size rounding and min-notional check.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from bobrito.config.settings import Settings
from bobrito.monitoring.logger import get_logger
from bobrito.monitoring.metrics import MetricsCollector
from bobrito.persistence.database import DatabaseManager
from bobrito.persistence.models import (
    Position,
    PositionStatus,
    RiskEvent,
    RiskEventType,
)
from bobrito.strategy.base import Signal

log = get_logger("risk.manager")


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    quantity: float = 0.0
    risk_amount: float = 0.0


@dataclass
class RiskViolation:
    event_type: RiskEventType
    description: str
    value: float | None = None
    threshold: float | None = None


# Binance BTCUSDT exchange filters (approximate; real values fetched from API)
STEP_SIZE = 0.00001
MIN_NOTIONAL = 5.0
MIN_QTY = 0.00001


class RiskManager:
    def __init__(self, settings: Settings, db: DatabaseManager) -> None:
        self._s = settings
        self._db = db

        # Runtime counters (reset each day)
        self._daily_trades: int = 0
        self._daily_realised_pnl: float = 0.0
        self._consecutive_losses: int = 0
        self._last_loss_time: datetime | None = None
        self._current_day: date = date.today()
        self._safe_mode: bool = False
        self._lock = asyncio.Lock()

        # Exchange symbol filters (set externally via configure_filters)
        self._step_size: float = STEP_SIZE
        self._min_qty: float = MIN_QTY
        self._min_notional: float = MIN_NOTIONAL

        # Runtime parameter overrides (take precedence over .env values).
        # Cleared by restore_defaults() or on process restart.
        self._overrides: dict[str, float | int] = {}

    # ── Startup bootstrap ─────────────────────────────────────────────────

    async def load_daily_stats(self) -> None:
        """Seed today's in-memory risk counters from closed positions in the DB.

        Restores daily_trades, daily_pnl, and the consecutive-loss streak so
        that risk limits remain correct after a process restart.
        """
        today = date.today()
        today_start = datetime.combine(today, datetime.min.time())

        async with self._db.session() as sess:
            # Today's closed trade count and realised PnL
            agg = await sess.execute(
                select(
                    func.count(Position.id).label("trades"),
                    func.coalesce(func.sum(Position.net_pnl), 0.0).label("pnl"),
                ).where(
                    Position.status == PositionStatus.CLOSED,
                    Position.closed_at >= today_start,
                )
            )
            row = agg.one()
            self._daily_trades = int(row.trades or 0)
            self._daily_realised_pnl = float(row.pnl or 0.0)

            # Consecutive loss streak: walk backward through the most recent
            # closed trades until we hit a winner or run out of rows.
            recent = await sess.execute(
                select(Position.net_pnl, Position.closed_at)
                .where(Position.status == PositionStatus.CLOSED)
                .order_by(Position.closed_at.desc())
                .limit(self._s.max_consecutive_losses + 10)
            )
            streak = 0
            last_loss_time: datetime | None = None
            for pnl, closed_at in recent.all():
                if pnl is not None and pnl < 0:
                    streak += 1
                    if last_loss_time is None:
                        last_loss_time = closed_at
                else:
                    break
            self._consecutive_losses = streak
            if last_loss_time is not None:
                self._last_loss_time = last_loss_time

        self._current_day = today
        log.info(
            f"Daily risk stats loaded from DB: trades={self._daily_trades} "
            f"pnl={self._daily_realised_pnl:.4f} USDT "
            f"consecutive_losses={self._consecutive_losses}"
        )

    # ── Configuration ─────────────────────────────────────────────────────

    def configure_filters(
        self,
        step_size: float,
        min_qty: float,
        min_notional: float,
    ) -> None:
        self._step_size = step_size
        self._min_qty = min_qty
        self._min_notional = min_notional
        log.info(
            f"Exchange filters set: step_size={step_size}, "
            f"min_qty={min_qty}, min_notional={min_notional}"
        )

    # ── Runtime parameter access ──────────────────────────────────────────

    def _p(self, name: str):
        """Return override value if set, else the .env default."""
        return self._overrides.get(name, getattr(self._s, name))

    def get_params(self) -> dict:
        """Current effective parameters with per-field override metadata."""
        keys = [
            "max_consecutive_losses",
            "max_daily_loss_pct",
            "cooldown_minutes_after_losses",
            "max_trades_per_day",
            "min_free_balance_usdt",
        ]
        return {
            k: {
                "value": self._p(k),
                "default": getattr(self._s, k),
                "overridden": k in self._overrides,
            }
            for k in keys
        }

    def set_params(
        self,
        max_consecutive_losses: int | None = None,
        max_daily_loss_pct: float | None = None,
        cooldown_minutes_after_losses: int | None = None,
        max_trades_per_day: int | None = None,
        min_free_balance_usdt: float | None = None,
    ) -> dict:
        """Override one or more risk parameters at runtime.

        Only non-None arguments are changed. Returns the updated params dict.
        Overrides persist until ``restore_defaults()`` is called or the
        process restarts.
        """
        mapping = {
            "max_consecutive_losses": max_consecutive_losses,
            "max_daily_loss_pct": max_daily_loss_pct,
            "cooldown_minutes_after_losses": cooldown_minutes_after_losses,
            "max_trades_per_day": max_trades_per_day,
            "min_free_balance_usdt": min_free_balance_usdt,
        }
        changed = [f"{k}={v}" for k, v in mapping.items() if v is not None]
        for k, v in mapping.items():
            if v is not None:
                self._overrides[k] = v
        if changed:
            log.info(f"Risk params overridden: {', '.join(changed)}")
        return self.get_params()

    def restore_defaults(self) -> dict:
        """Clear all runtime overrides — revert every parameter to its .env value."""
        self._overrides.clear()
        log.info("Risk params restored to .env defaults")
        return self.get_params()

    # ── Counter resets ────────────────────────────────────────────────────

    def reset_cooldown(self) -> None:
        """Manually clear the post-loss cooldown timer."""
        self._last_loss_time = None
        log.info("Cooldown timer manually reset")

    def reset_consecutive_losses(self) -> None:
        """Manually reset the consecutive-loss streak to zero."""
        self._consecutive_losses = 0
        log.info("Consecutive loss counter manually reset to 0")

    def reset_daily_counters(self) -> None:
        """Manually reset today's trade count and realised PnL to zero."""
        self._daily_trades = 0
        self._daily_realised_pnl = 0.0
        log.info("Daily counters (trades + PnL) manually reset")

    def reset_all_counters(self) -> None:
        """Reset every limiter counter at once."""
        self.reset_cooldown()
        self.reset_consecutive_losses()
        self.reset_daily_counters()
        log.info("All risk counters reset")

    # ── Midnight auto-reset ───────────────────────────────────────────────

    async def run_midnight_reset_loop(self) -> None:
        """Background task: reset daily counters exactly at midnight UTC.

        Sleeps until the next 00:00:00 UTC, fires the reset, then loops.
        Started as an asyncio.Task by the bot engine on startup.
        """
        while True:
            now = datetime.utcnow()
            tomorrow = datetime(now.year, now.month, now.day) + timedelta(days=1)
            sleep_secs = (tomorrow - now).total_seconds()
            log.info(
                f"Midnight reset scheduled in "
                f"{int(sleep_secs // 3600)}h {int((sleep_secs % 3600) // 60)}m"
            )
            await asyncio.sleep(sleep_secs)
            async with self._lock:
                self._daily_trades = 0
                self._daily_realised_pnl = 0.0
                self._current_day = date.today()
                log.info(f"Midnight UTC: daily counters reset for {self._current_day}")

    # ── Entry validation ──────────────────────────────────────────────────

    async def validate_entry(
        self,
        signal: Signal,
        free_usdt: float,
        has_open_position: bool,
    ) -> RiskDecision:
        async with self._lock:
            self._maybe_reset_daily()

            # Hard gate: safe mode
            if self._safe_mode:
                return RiskDecision(allowed=False, reason="Safe mode active — all entries blocked")

            if has_open_position:
                return RiskDecision(allowed=False, reason="Existing open position")

            violations = await self._check_rules(free_usdt)
            if violations:
                v = violations[0]
                await self._persist_risk_event(v)
                MetricsCollector.risk_events_total.labels(event_type=v.event_type.value).inc()
                return RiskDecision(allowed=False, reason=v.description)

            if signal.stop_price is None:
                return RiskDecision(allowed=False, reason="Signal missing stop price")

            stop_distance = signal.price - signal.stop_price
            if stop_distance <= 0:
                return RiskDecision(allowed=False, reason="Invalid stop distance (≤ 0)")

            qty, risk_amount = self._calculate_position_size(
                signal.price, stop_distance, free_usdt
            )
            if qty <= 0:
                return RiskDecision(
                    allowed=False, reason="Calculated quantity too small for exchange filters"
                )

            notional = qty * signal.price
            if notional < self._min_notional:
                return RiskDecision(
                    allowed=False,
                    reason=f"Notional {notional:.2f} < min_notional {self._min_notional}",
                )

            if notional > free_usdt - self._p("min_free_balance_usdt"):
                return RiskDecision(
                    allowed=False,
                    reason=f"Insufficient free balance ({free_usdt:.2f} USDT)",
                )

            return RiskDecision(allowed=True, reason="OK", quantity=qty, risk_amount=risk_amount)

    # ── Trade outcome recording ───────────────────────────────────────────

    async def record_trade_result(self, pnl: float) -> None:
        async with self._lock:
            self._maybe_reset_daily()
            self._daily_trades += 1
            self._daily_realised_pnl += pnl
            if pnl < 0:
                self._consecutive_losses += 1
                self._last_loss_time = datetime.utcnow()
                log.warning(
                    f"Loss recorded: pnl={pnl:.4f} USDT, "
                    f"consecutive_losses={self._consecutive_losses}"
                )
            else:
                self._consecutive_losses = 0
                log.info(f"Win recorded: pnl={pnl:.4f} USDT")

    def activate_safe_mode(self, reason: str) -> None:
        self._safe_mode = True
        log.warning(f"SAFE MODE ACTIVATED: {reason}")

    def deactivate_safe_mode(self) -> None:
        self._safe_mode = False
        log.info("Safe mode deactivated")

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def safe_mode(self) -> bool:
        return self._safe_mode

    @property
    def daily_trades(self) -> int:
        return self._daily_trades

    @property
    def daily_pnl(self) -> float:
        return self._daily_realised_pnl

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    def state_dict(self) -> dict:
        return {
            "safe_mode": self._safe_mode,
            "daily_trades": self._daily_trades,
            "daily_pnl": round(self._daily_realised_pnl, 4),
            "consecutive_losses": self._consecutive_losses,
            "current_day": str(self._current_day),
        }

    def check_entry_blocks(self, free_usdt: float | None = None) -> list[dict]:
        """Return all currently active entry-blocking conditions.

        Synchronous and side-effect-free — reads in-memory counters only.
        Each item is a dict with keys: type, name, reason, reset_tip, severity,
        and optionally remaining_seconds (cooldown only).

        Used by the UI layer to show accurate operational status.
        """
        blocks: list[dict] = []
        now = datetime.utcnow()
        is_new_day = date.today() != self._current_day

        max_daily_loss_pct = self._p("max_daily_loss_pct")
        max_trades_per_day = self._p("max_trades_per_day")
        max_consecutive_losses = self._p("max_consecutive_losses")
        cooldown_minutes = self._p("cooldown_minutes_after_losses")
        min_free_balance = self._p("min_free_balance_usdt")

        # Safe mode (survives restarts)
        if self._safe_mode:
            blocks.append({
                "type": "safe_mode",
                "name": "Safe Mode",
                "reason": "A critical runtime error forced the bot into safe mode. All new entries are blocked.",
                "reset_tip": "Restart the bot process to clear safe mode.",
                "severity": "critical",
            })

        # Daily counters only apply for the current trading day
        if not is_new_day:
            capital = self._s.initial_capital_usdt
            max_daily_loss = capital * max_daily_loss_pct / 100
            if self._daily_realised_pnl <= -max_daily_loss:
                blocks.append({
                    "type": "daily_loss",
                    "name": "Daily Loss Limit",
                    "reason": (
                        f"Today's realised loss ({abs(self._daily_realised_pnl):.4f} USDT) "
                        f"reached the {max_daily_loss_pct}% daily limit "
                        f"({max_daily_loss:.2f} USDT on {capital:.0f} USDT capital)."
                    ),
                    "reset_tip": "Resets automatically at midnight UTC.",
                    "severity": "critical",
                })

            if self._daily_trades >= max_trades_per_day:
                blocks.append({
                    "type": "max_trades",
                    "name": "Daily Trade Limit",
                    "reason": (
                        f"{self._daily_trades} trades executed today "
                        f"(configured limit: {max_trades_per_day})."
                    ),
                    "reset_tip": "Resets automatically at midnight UTC.",
                    "severity": "warning",
                })

        # Consecutive losses (persists across days)
        if self._consecutive_losses >= max_consecutive_losses:
            blocks.append({
                "type": "consecutive_losses",
                "name": "Consecutive Loss Limit",
                "reason": (
                    f"{self._consecutive_losses} consecutive losing trades "
                    f"reached the limit of {max_consecutive_losses}."
                ),
                "reset_tip": (
                    "Clears automatically after the next profitable trade closes. "
                    f"Current losing streak: {self._consecutive_losses}."
                ),
                "severity": "warning",
            })

        # Cooldown timer
        if self._last_loss_time and cooldown_minutes > 0:
            cooldown_end = self._last_loss_time + timedelta(minutes=cooldown_minutes)
            if now < cooldown_end:
                remaining = int((cooldown_end - now).total_seconds())
                mins, secs = divmod(remaining, 60)
                blocks.append({
                    "type": "cooldown",
                    "name": "Post-Loss Cooldown",
                    "reason": (
                        f"Mandatory cooldown period is active after a losing trade. "
                        f"{mins}m {secs:02d}s remaining out of {cooldown_minutes} min total."
                    ),
                    "reset_tip": f"Lifts automatically in {mins}m {secs:02d}s. No action needed.",
                    "severity": "warning",
                    "remaining_seconds": remaining,
                })

        # Minimum free balance (optional — only checked when balance is known)
        if free_usdt is not None and free_usdt < min_free_balance:
            blocks.append({
                "type": "min_balance",
                "name": "Minimum Balance Reserve",
                "reason": (
                    f"Free USDT ({free_usdt:.2f}) is below the required minimum "
                    f"({min_free_balance:.2f} USDT)."
                ),
                "reset_tip": (
                    "Recovers automatically when the open position is closed "
                    "and USDT is returned to the account."
                ),
                "severity": "warning",
            })

        return blocks

    # ── Private helpers ───────────────────────────────────────────────────

    def _maybe_reset_daily(self) -> None:
        today = date.today()
        if today != self._current_day:
            log.info(f"New trading day {today}. Resetting daily counters.")
            self._daily_trades = 0
            self._daily_realised_pnl = 0.0
            self._current_day = today

    async def _check_rules(self, free_usdt: float) -> list[RiskViolation]:
        violations: list[RiskViolation] = []

        max_daily_loss_pct = self._p("max_daily_loss_pct")
        max_consecutive_losses = self._p("max_consecutive_losses")
        cooldown_minutes = self._p("cooldown_minutes_after_losses")
        max_trades_per_day = self._p("max_trades_per_day")
        min_free_balance = self._p("min_free_balance_usdt")

        # Daily loss limit
        capital = self._s.initial_capital_usdt
        max_daily_loss = capital * max_daily_loss_pct / 100
        if self._daily_realised_pnl <= -max_daily_loss:
            violations.append(
                RiskViolation(
                    event_type=RiskEventType.DAILY_LOSS_LIMIT,
                    description=(
                        f"Daily loss limit reached: "
                        f"{self._daily_realised_pnl:.2f} ≤ -{max_daily_loss:.2f} USDT"
                    ),
                    value=abs(self._daily_realised_pnl),
                    threshold=max_daily_loss,
                )
            )

        # Consecutive losses
        if self._consecutive_losses >= max_consecutive_losses:
            violations.append(
                RiskViolation(
                    event_type=RiskEventType.CONSECUTIVE_LOSSES,
                    description=(
                        f"Consecutive loss limit: {self._consecutive_losses} "
                        f"≥ {max_consecutive_losses}"
                    ),
                    value=float(self._consecutive_losses),
                    threshold=float(max_consecutive_losses),
                )
            )

        # Cooldown
        if self._last_loss_time and cooldown_minutes > 0:
            cooldown_end = self._last_loss_time + timedelta(minutes=cooldown_minutes)
            if datetime.utcnow() < cooldown_end:
                remaining = (cooldown_end - datetime.utcnow()).seconds // 60
                violations.append(
                    RiskViolation(
                        event_type=RiskEventType.COOLDOWN,
                        description=f"Cooldown active — {remaining} min remaining",
                    )
                )

        # Max trades per day
        if self._daily_trades >= max_trades_per_day:
            violations.append(
                RiskViolation(
                    event_type=RiskEventType.MAX_TRADES,
                    description=(
                        f"Max trades/day reached: {self._daily_trades} "
                        f"≥ {max_trades_per_day}"
                    ),
                    value=float(self._daily_trades),
                    threshold=float(max_trades_per_day),
                )
            )

        # Minimum free balance
        if free_usdt < min_free_balance:
            violations.append(
                RiskViolation(
                    event_type=RiskEventType.MIN_BALANCE,
                    description=(
                        f"Free balance {free_usdt:.2f} < "
                        f"minimum {min_free_balance:.2f} USDT"
                    ),
                    value=free_usdt,
                    threshold=min_free_balance,
                )
            )

        return violations

    def _calculate_position_size(
        self,
        price: float,
        stop_distance: float,
        free_usdt: float,
    ) -> tuple[float, float]:
        """Fixed-fractional position sizing on actual current equity.

        Uses the actual available balance (free_usdt minus the required
        reserve) as the capital base so that position sizes scale correctly
        after profits or losses rather than always anchoring to the initial
        deposit.

        Returns (quantity, risk_amount_usdt).
        """
        tradeable = free_usdt - self._p("min_free_balance_usdt")
        if tradeable <= 0:
            return 0.0, 0.0

        risk_amount = tradeable * self._s.risk_per_trade_pct / 100
        raw_qty = risk_amount / stop_distance
        qty = _round_step(raw_qty, self._step_size)
        qty = max(qty, self._min_qty)

        # Hard cap: never spend more than the full tradeable balance
        max_affordable_qty = _round_step(tradeable / price, self._step_size)
        qty = min(qty, max_affordable_qty)
        return qty, risk_amount

    async def _persist_risk_event(self, v: RiskViolation) -> None:
        mode = self._s.bot_mode
        mode_str = mode.value if hasattr(mode, "value") else str(mode)
        event = RiskEvent(
            event_type=v.event_type,
            description=v.description,
            value=v.value,
            threshold=v.threshold,
            mode=mode_str,
        )
        async with self._db.session() as sess:
            sess.add(event)
            await sess.commit()


def _round_step(qty: float, step: float) -> float:
    """Round qty down to the nearest step increment."""
    if step <= 0:
        return qty
    precision = max(0, int(round(-math.log10(step))))
    factor = 10 ** precision
    return math.floor(qty * factor) / factor
