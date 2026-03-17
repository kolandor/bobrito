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

Runtime overrides: limits 2–6 can be changed via API/UI without restarting.
All overrides revert to ENV-file defaults automatically at midnight UTC.
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

        # Runtime overrides — None means "use ENV value"
        # All revert to None automatically at midnight UTC.
        self._max_consecutive_losses_override: int | None = None
        self._max_daily_loss_pct_override: float | None = None
        self._min_free_balance_usdt_override: float | None = None
        self._max_trades_per_day_override: int | None = None

        # Exchange symbol filters (set externally via configure_filters)
        self._step_size: float = STEP_SIZE
        self._min_qty: float = MIN_QTY
        self._min_notional: float = MIN_NOTIONAL

    # ── Effective limit properties ─────────────────────────────────────────

    @property
    def _eff_max_consecutive_losses(self) -> int:
        if self._max_consecutive_losses_override is not None:
            return self._max_consecutive_losses_override
        return self._s.max_consecutive_losses

    @property
    def _eff_max_daily_loss_pct(self) -> float:
        if self._max_daily_loss_pct_override is not None:
            return self._max_daily_loss_pct_override
        return self._s.max_daily_loss_pct

    @property
    def _eff_min_free_balance_usdt(self) -> float:
        if self._min_free_balance_usdt_override is not None:
            return self._min_free_balance_usdt_override
        return self._s.min_free_balance_usdt

    @property
    def _eff_max_trades_per_day(self) -> int:
        if self._max_trades_per_day_override is not None:
            return self._max_trades_per_day_override
        return self._s.max_trades_per_day

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

            if notional > free_usdt - self._eff_min_free_balance_usdt:
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

    # ── Runtime limit management ──────────────────────────────────────────

    async def reset_cooldown(self) -> None:
        """Clear the active post-loss cooldown timer.

        This is a conscious operator override — use when you have reviewed
        the situation and want to allow new entries before the cooldown expires.
        The consecutive loss streak is unaffected; only the timer is cleared.
        """
        async with self._lock:
            self._last_loss_time = None
        log.info("Post-loss cooldown timer manually reset by operator (consecutive loss streak unchanged).")

    def set_max_consecutive_losses(self, value: int) -> None:
        """Override the max consecutive losses limit (session-scoped)."""
        self._max_consecutive_losses_override = value
        log.info(f"max_consecutive_losses overridden to {value} (default: {self._s.max_consecutive_losses})")

    def set_max_daily_loss_pct(self, value: float) -> None:
        """Override the daily PnL loss limit in percent (session-scoped)."""
        self._max_daily_loss_pct_override = value
        log.info(f"max_daily_loss_pct overridden to {value}% (default: {self._s.max_daily_loss_pct}%)")

    def set_min_free_balance_usdt(self, value: float) -> None:
        """Override the minimum free balance reserve in USDT (session-scoped)."""
        self._min_free_balance_usdt_override = value
        log.info(f"min_free_balance_usdt overridden to {value} (default: {self._s.min_free_balance_usdt})")

    def set_max_trades_per_day(self, value: int) -> None:
        """Override the daily trade count limit (session-scoped)."""
        self._max_trades_per_day_override = value
        log.info(f"max_trades_per_day overridden to {value} (default: {self._s.max_trades_per_day})")

    def restore_defaults(self) -> None:
        """Restore all runtime limit overrides to their ENV-file values."""
        self._max_consecutive_losses_override = None
        self._max_daily_loss_pct_override = None
        self._min_free_balance_usdt_override = None
        self._max_trades_per_day_override = None
        log.info("Risk limit overrides cleared — reverting to ENV-file defaults.")

    async def run_midnight_reset_loop(self) -> None:
        """Background task: reset daily counters and clear overrides at midnight UTC.

        Provides reliable midnight resets independent of trade activity.
        Cancelled automatically when the bot stops.
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
                self.restore_defaults()
                log.info(f"Midnight UTC: daily counters + overrides reset for {self._current_day}")

    def has_overrides(self) -> bool:
        """Return True if any limit is currently overridden from its ENV default."""
        return any([
            self._max_consecutive_losses_override is not None,
            self._max_daily_loss_pct_override is not None,
            self._min_free_balance_usdt_override is not None,
            self._max_trades_per_day_override is not None,
        ])

    def limits_dict(self) -> dict:
        """Return current effective and default limit values."""
        return {
            "effective": {
                "max_consecutive_losses": self._eff_max_consecutive_losses,
                "max_daily_loss_pct": self._eff_max_daily_loss_pct,
                "min_free_balance_usdt": self._eff_min_free_balance_usdt,
                "max_trades_per_day": self._eff_max_trades_per_day,
            },
            "defaults": {
                "max_consecutive_losses": self._s.max_consecutive_losses,
                "max_daily_loss_pct": self._s.max_daily_loss_pct,
                "min_free_balance_usdt": self._s.min_free_balance_usdt,
                "max_trades_per_day": self._s.max_trades_per_day,
            },
            "has_overrides": self.has_overrides(),
        }

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
            "limits": {
                "max_consecutive_losses": self._eff_max_consecutive_losses,
                "max_daily_loss_pct": self._eff_max_daily_loss_pct,
                "min_free_balance_usdt": self._eff_min_free_balance_usdt,
                "max_trades_per_day": self._eff_max_trades_per_day,
            },
            "defaults": {
                "max_consecutive_losses": self._s.max_consecutive_losses,
                "max_daily_loss_pct": self._s.max_daily_loss_pct,
                "min_free_balance_usdt": self._s.min_free_balance_usdt,
                "max_trades_per_day": self._s.max_trades_per_day,
            },
            "has_overrides": self.has_overrides(),
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
            max_daily_loss = capital * self._eff_max_daily_loss_pct / 100
            if self._daily_realised_pnl <= -max_daily_loss:
                blocks.append({
                    "type": "daily_loss",
                    "name": "Daily Loss Limit",
                    "reason": (
                        f"Today's realised loss ({abs(self._daily_realised_pnl):.4f} USDT) "
                        f"reached the {self._eff_max_daily_loss_pct}% daily limit "
                        f"({max_daily_loss:.2f} USDT on {capital:.0f} USDT capital)."
                    ),
                    "reset_tip": "Resets automatically at midnight UTC (start of the next trading day).",
                    "severity": "critical",
                })

            if self._daily_trades >= self._eff_max_trades_per_day:
                blocks.append({
                    "type": "max_trades",
                    "name": "Daily Trade Limit",
                    "reason": (
                        f"{self._daily_trades} trades executed today "
                        f"(configured limit: {self._eff_max_trades_per_day})."
                    ),
                    "reset_tip": "Resets automatically at midnight UTC (start of the next trading day).",
                    "severity": "warning",
                })

        # Consecutive losses (persists across days)
        if self._consecutive_losses >= self._eff_max_consecutive_losses:
            blocks.append({
                "type": "consecutive_losses",
                "name": "Consecutive Loss Limit",
                "reason": (
                    f"{self._consecutive_losses} consecutive losing trades "
                    f"reached the limit of {self._eff_max_consecutive_losses}."
                ),
                "reset_tip": (
                    "Clears automatically after the next profitable trade closes. "
                    f"Current losing streak: {self._consecutive_losses}."
                ),
                "severity": "warning",
            })

        # Cooldown timer
        if self._last_loss_time and self._s.cooldown_minutes_after_losses > 0:
            cooldown_end = self._last_loss_time + timedelta(
                minutes=self._s.cooldown_minutes_after_losses
            )
            if now < cooldown_end:
                remaining = int((cooldown_end - now).total_seconds())
                mins, secs = divmod(remaining, 60)
                blocks.append({
                    "type": "cooldown",
                    "name": "Post-Loss Cooldown",
                    "reason": (
                        f"Mandatory cooldown period is active after a losing trade. "
                        f"{mins}m {secs:02d}s remaining out of "
                        f"{self._s.cooldown_minutes_after_losses} min total."
                    ),
                    "reset_tip": f"Lifts automatically in {mins}m {secs:02d}s. No action needed.",
                    "severity": "warning",
                    "remaining_seconds": remaining,
                })

        # Minimum free balance (optional — only checked when balance is known)
        if free_usdt is not None and free_usdt < self._eff_min_free_balance_usdt:
            blocks.append({
                "type": "min_balance",
                "name": "Minimum Balance Reserve",
                "reason": (
                    f"Free USDT ({free_usdt:.2f}) is below the required minimum "
                    f"({self._eff_min_free_balance_usdt:.2f} USDT)."
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
            log.info(
                f"New trading day {today}. Resetting daily counters and reverting limit overrides."
            )
            self._daily_trades = 0
            self._daily_realised_pnl = 0.0
            self._current_day = today
            # Restore all limit overrides to ENV defaults at midnight
            self.restore_defaults()

    async def _check_rules(self, free_usdt: float) -> list[RiskViolation]:
        violations: list[RiskViolation] = []

        # Daily loss limit
        capital = self._s.initial_capital_usdt
        max_daily_loss = capital * self._eff_max_daily_loss_pct / 100
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
        if self._consecutive_losses >= self._eff_max_consecutive_losses:
            violations.append(
                RiskViolation(
                    event_type=RiskEventType.CONSECUTIVE_LOSSES,
                    description=(
                        f"Consecutive loss limit: {self._consecutive_losses} "
                        f"≥ {self._eff_max_consecutive_losses}"
                    ),
                    value=float(self._consecutive_losses),
                    threshold=float(self._eff_max_consecutive_losses),
                )
            )

        # Cooldown
        if self._last_loss_time and self._s.cooldown_minutes_after_losses > 0:
            cooldown_end = self._last_loss_time + timedelta(
                minutes=self._s.cooldown_minutes_after_losses
            )
            if datetime.utcnow() < cooldown_end:
                remaining = (cooldown_end - datetime.utcnow()).seconds // 60
                violations.append(
                    RiskViolation(
                        event_type=RiskEventType.COOLDOWN,
                        description=f"Cooldown active — {remaining} min remaining",
                    )
                )

        # Max trades per day
        if self._daily_trades >= self._eff_max_trades_per_day:
            violations.append(
                RiskViolation(
                    event_type=RiskEventType.MAX_TRADES,
                    description=(
                        f"Max trades/day reached: {self._daily_trades} "
                        f"≥ {self._eff_max_trades_per_day}"
                    ),
                    value=float(self._daily_trades),
                    threshold=float(self._eff_max_trades_per_day),
                )
            )

        # Minimum free balance
        if free_usdt < self._eff_min_free_balance_usdt:
            violations.append(
                RiskViolation(
                    event_type=RiskEventType.MIN_BALANCE,
                    description=(
                        f"Free balance {free_usdt:.2f} < "
                        f"minimum {self._eff_min_free_balance_usdt:.2f} USDT"
                    ),
                    value=free_usdt,
                    threshold=self._eff_min_free_balance_usdt,
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
        tradeable = free_usdt - self._eff_min_free_balance_usdt
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
