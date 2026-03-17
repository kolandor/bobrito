"""SQLAlchemy ORM models for all persisted entities."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ── Enum types ──────────────────────────────────────────────────────────────

class SignalType(str, enum.Enum):
    BUY = "BUY"
    EXIT = "EXIT"
    HOLD = "HOLD"


class OrderSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, enum.Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"


class PositionStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class ExitReason(str, enum.Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    MOMENTUM_FAILURE = "MOMENTUM_FAILURE"
    EMERGENCY = "EMERGENCY"
    SESSION_CLOSE = "SESSION_CLOSE"
    MANUAL = "MANUAL"


class RiskEventType(str, enum.Enum):
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES"
    COOLDOWN = "COOLDOWN"
    MAX_TRADES = "MAX_TRADES"
    MIN_BALANCE = "MIN_BALANCE"
    SAFE_MODE = "SAFE_MODE"
    MIN_TARGET_DISTANCE = "MIN_TARGET_DISTANCE"
    MIN_EXPECTED_EDGE = "MIN_EXPECTED_EDGE"


class BotMode(str, enum.Enum):
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


# ── Signal ──────────────────────────────────────────────────────────────────

class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    signal_type: Mapped[str] = mapped_column(SAEnum(SignalType), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    atr: Mapped[float | None] = mapped_column(Float, nullable=True)
    ema_fast: Mapped[float | None] = mapped_column(Float, nullable=True)
    ema_slow: Mapped[float | None] = mapped_column(Float, nullable=True)
    regime: Mapped[str | None] = mapped_column(String(20), nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    acted_on: Mapped[bool] = mapped_column(Boolean, default=False)
    rejected_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (Index("ix_signals_created_at", "created_at"),)


# ── Order ───────────────────────────────────────────────────────────────────

class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    side: Mapped[str] = mapped_column(SAEnum(OrderSide), nullable=False)
    order_type: Mapped[str] = mapped_column(SAEnum(OrderType), nullable=False)
    status: Mapped[str] = mapped_column(
        SAEnum(OrderStatus), nullable=False, default=OrderStatus.PENDING
    )
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    average_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    commission_asset: Mapped[str] = mapped_column(String(10), default="USDT")
    position_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("positions.id"), nullable=True
    )

    fills: Mapped[list[Fill]] = relationship("Fill", back_populates="order")
    position: Mapped[Position | None] = relationship("Position", back_populates="orders")

    __table_args__ = (
        Index("ix_orders_created_at", "created_at"),
        Index("ix_orders_status", "status"),
    )


# ── Fill ────────────────────────────────────────────────────────────────────

class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id"), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    commission_asset: Mapped[str] = mapped_column(String(10), default="USDT")

    order: Mapped[Order] = relationship("Order", back_populates="fills")


# ── Position ─────────────────────────────────────────────────────────────────

class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(
        SAEnum(PositionStatus), nullable=False, default=PositionStatus.OPEN
    )
    side: Mapped[str] = mapped_column(SAEnum(OrderSide), nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    stop_price: Mapped[float] = mapped_column(Float, nullable=False)
    target_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(SAEnum(ExitReason), nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_fees: Mapped[float] = mapped_column(Float, default=0.0)
    net_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("signals.id"), nullable=True
    )

    orders: Mapped[list[Order]] = relationship("Order", back_populates="position")

    __table_args__ = (Index("ix_positions_opened_at", "opened_at"),)


# ── Balance Snapshot ─────────────────────────────────────────────────────────

class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    free_usdt: Mapped[float] = mapped_column(Float, nullable=False)
    locked_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    free_btc: Mapped[float] = mapped_column(Float, default=0.0)
    locked_btc: Mapped[float] = mapped_column(Float, default=0.0)
    btc_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    equity_usdt: Mapped[float] = mapped_column(Float, nullable=False)
    open_position_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


# ── Risk Event ───────────────────────────────────────────────────────────────

class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    event_type: Mapped[str] = mapped_column(SAEnum(RiskEventType), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)


# ── System Event ─────────────────────────────────────────────────────────────

class SystemEvent(Base):
    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)


# ── Error Log ────────────────────────────────────────────────────────────────

class ErrorLog(Base):
    __tablename__ = "error_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    module: Mapped[str] = mapped_column(String(60), nullable=False)
    error_type: Mapped[str] = mapped_column(String(100), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    traceback: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
