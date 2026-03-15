"""Portfolio Manager.

Tracks:
  - open position lifecycle (entry → exit)
  - running PnL
  - equity snapshots to the database
  - cumulative win/loss statistics

All mutations are async-safe via an internal lock.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select

from bobrito.config.settings import Settings
from bobrito.execution.base import OrderResult
from bobrito.monitoring.logger import get_logger
from bobrito.monitoring.metrics import MetricsCollector
from bobrito.persistence.database import DatabaseManager
from bobrito.persistence.models import (
    BalanceSnapshot,
    ExitReason,
    PositionStatus,
)
from bobrito.persistence.models import (
    Position as DBPosition,
)

log = get_logger("portfolio.manager")


@dataclass
class PositionState:
    """In-memory representation of an open position."""

    db_id: int | None
    symbol: str
    side: str
    entry_price: float
    quantity: float
    stop_price: float
    target_price: float
    entry_time: datetime
    fees: float = 0.0
    unrealised_pnl: float = 0.0
    signal_id: int | None = None

    def update_unrealised(self, current_price: float) -> None:
        raw = (current_price - self.entry_price) * self.quantity
        self.unrealised_pnl = raw - self.fees

    def is_stop_hit(self, price: float) -> bool:
        return price <= self.stop_price

    def is_target_hit(self, price: float) -> bool:
        return price >= self.target_price


class PortfolioManager:
    def __init__(self, settings: Settings, db: DatabaseManager) -> None:
        self._s = settings
        self._db = db
        self._lock = asyncio.Lock()

        self._open_position: PositionState | None = None

        # Running stats
        self._total_trades = 0
        self._wins = 0
        self._losses = 0
        self._total_pnl: float = 0.0
        self._peak_equity: float = settings.initial_capital_usdt
        self._max_drawdown_pct: float = 0.0

    # ── Startup bootstrap ─────────────────────────────────────────────────

    async def load_historical_stats(self) -> None:
        """Seed in-memory counters from closed positions stored in the database.

        Called once at bot startup so the dashboard always shows lifetime
        totals, not just the totals for the current process run.
        """
        async with self._db.session() as sess:
            result = await sess.execute(
                select(
                    func.count(DBPosition.id).label("total"),
                    func.coalesce(func.sum(DBPosition.net_pnl), 0.0).label("total_pnl"),
                    func.coalesce(
                        func.sum(case((DBPosition.net_pnl > 0, 1), else_=0)), 0
                    ).label("wins"),
                    func.coalesce(
                        func.sum(case((DBPosition.net_pnl <= 0, 1), else_=0)), 0
                    ).label("losses"),
                ).where(DBPosition.status == PositionStatus.CLOSED)
            )
            row = result.one()

        self._total_trades = int(row.total or 0)
        self._total_pnl = float(row.total_pnl or 0.0)
        self._wins = int(row.wins or 0)
        self._losses = int(row.losses or 0)

        log.info(
            f"Historical stats loaded from DB: trades={self._total_trades} "
            f"wins={self._wins} losses={self._losses} "
            f"total_pnl={self._total_pnl:.4f} USDT"
        )

    # ── Accessors ─────────────────────────────────────────────────────────

    def has_open_position(self) -> bool:
        return self._open_position is not None

    def get_open_position(self) -> PositionState | None:
        return self._open_position

    def stats(self) -> dict:
        wr = self._wins / self._total_trades * 100 if self._total_trades else 0.0
        return {
            "total_trades": self._total_trades,
            "wins": self._wins,
            "losses": self._losses,
            "win_rate_pct": round(wr, 2),
            "total_pnl_usdt": round(self._total_pnl, 4),
            "max_drawdown_pct": round(self._max_drawdown_pct, 2),
        }

    # ── Position lifecycle ─────────────────────────────────────────────────

    async def open_position(
        self,
        order: OrderResult,
        stop_price: float,
        target_price: float,
        risk_amount: float,
        signal_id: int | None = None,
    ) -> PositionState:
        async with self._lock:
            if self._open_position is not None:
                raise RuntimeError("Cannot open position: one already exists")

            fees = order.commission
            pos = PositionState(
                db_id=None,
                symbol=order.symbol,
                side=order.side.value,
                entry_price=order.average_price,
                quantity=order.filled_qty,
                stop_price=stop_price,
                target_price=target_price,
                entry_time=order.timestamp,
                fees=fees,
                signal_id=signal_id,
            )

            db_pos = DBPosition(
                symbol=order.symbol,
                mode=self._s.bot_mode.value,
                status=PositionStatus.OPEN,
                side=order.side.value,
                entry_price=order.average_price,
                quantity=order.filled_qty,
                stop_price=stop_price,
                target_price=target_price,
                total_fees=fees,
                risk_amount=risk_amount,
                signal_id=signal_id,
            )
            async with self._db.session() as sess:
                sess.add(db_pos)
                await sess.commit()
                await sess.refresh(db_pos)

            pos.db_id = db_pos.id
            self._open_position = pos
            MetricsCollector.open_position.set(1)

            log.info(
                f"Position OPENED | entry={order.average_price:.2f} "
                f"qty={order.filled_qty:.6f} stop={stop_price:.2f} "
                f"target={target_price:.2f}"
            )
            return pos

    async def close_position(
        self,
        order: OrderResult,
        reason: ExitReason,
        current_equity: float,
    ) -> float:
        """Returns realised PnL (net of fees)."""
        async with self._lock:
            if self._open_position is None:
                raise RuntimeError("No open position to close")

            pos = self._open_position
            exit_price = order.average_price
            exit_fees = order.commission

            gross_pnl = (exit_price - pos.entry_price) * pos.quantity
            total_fees = pos.fees + exit_fees
            net_pnl = gross_pnl - total_fees

            # Update DB
            async with self._db.session() as sess:
                db_pos = await sess.get(DBPosition, pos.db_id)
                if db_pos:
                    db_pos.status = PositionStatus.CLOSED
                    db_pos.exit_price = exit_price
                    db_pos.exit_reason = reason
                    db_pos.realized_pnl = gross_pnl
                    db_pos.total_fees = total_fees
                    db_pos.net_pnl = net_pnl
                    db_pos.closed_at = datetime.utcnow()
                    await sess.commit()

            # Update stats
            self._total_trades += 1
            self._total_pnl += net_pnl
            if net_pnl > 0:
                self._wins += 1
                MetricsCollector.wins_total.labels(mode=self._s.bot_mode.value).inc()
            else:
                self._losses += 1
                MetricsCollector.losses_total.labels(mode=self._s.bot_mode.value).inc()

            # Drawdown tracking
            new_equity = current_equity + net_pnl
            if new_equity > self._peak_equity:
                self._peak_equity = new_equity
            elif self._peak_equity > 0:
                dd = (self._peak_equity - new_equity) / self._peak_equity * 100
                self._max_drawdown_pct = max(self._max_drawdown_pct, dd)

            MetricsCollector.pnl_realised_usdt.set(self._total_pnl)
            MetricsCollector.max_drawdown_pct.set(self._max_drawdown_pct)
            MetricsCollector.open_position.set(0)

            self._open_position = None
            log.info(
                f"Position CLOSED | exit={exit_price:.2f} reason={reason.value} "
                f"pnl={net_pnl:.4f} USDT"
            )
            return net_pnl

    # ── Equity snapshot ───────────────────────────────────────────────────

    async def snapshot_equity(
        self,
        free_usdt: float,
        free_btc: float,
        btc_price: float,
        locked_usdt: float = 0.0,
        locked_btc: float = 0.0,
    ) -> None:
        equity = free_usdt + locked_usdt + (free_btc + locked_btc) * btc_price
        MetricsCollector.equity_usdt.set(equity)

        snap = BalanceSnapshot(
            mode=self._s.bot_mode.value,
            free_usdt=free_usdt,
            locked_usdt=locked_usdt,
            free_btc=free_btc,
            locked_btc=locked_btc,
            btc_price=btc_price,
            equity_usdt=equity,
            open_position_id=self._open_position.db_id if self._open_position else None,
        )
        async with self._db.session() as sess:
            sess.add(snap)
            await sess.commit()
