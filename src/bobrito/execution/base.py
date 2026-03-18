"""Unified broker interface.

All broker implementations must subclass BrokerBase so that the strategy
and bot engine remain broker-agnostic.

SymbolFilters is the canonical typed model for exchange symbol constraints.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass
class OrderRequest:
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    price: float | None = None  # Limit price (None = market)
    client_order_id: str = ""
    stop_price: float | None = None


@dataclass
class OrderResult:
    client_order_id: str
    exchange_order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    status: OrderStatus
    requested_qty: float
    filled_qty: float = 0.0
    average_price: float = 0.0
    commission: float = 0.0
    commission_asset: str = "USDT"
    timestamp: datetime = field(default_factory=datetime.utcnow)
    raw: dict = field(default_factory=dict)

    @property
    def is_filled(self) -> bool:
        return self.status == OrderStatus.FILLED

    @property
    def notional(self) -> float:
        return self.filled_qty * self.average_price


@dataclass(frozen=True)
class SymbolFilters:
    """Canonical exchange symbol constraints. Use Decimal for precision-critical ops."""

    symbol: str
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal
    tick_size: Decimal

    def quantize_qty(self, qty: float | Decimal) -> Decimal:
        """Round quantity down to step_size precision. Decimal-safe: never uses float internally."""
        d = qty if isinstance(qty, Decimal) else Decimal(str(qty))
        return d.quantize(self.step_size, rounding=ROUND_DOWN)

    def quantize_price(self, price: float | Decimal) -> Decimal:
        """Round price to tick_size precision. Decimal-safe: never uses float internally."""
        d = price if isinstance(price, Decimal) else Decimal(str(price))
        return d.quantize(self.tick_size, rounding=ROUND_DOWN)

    def check_qty(self, qty: Decimal) -> bool:
        """True if quantity meets min_qty and step alignment."""
        return qty >= self.min_qty and qty == self.quantize_qty(qty)

    def check_notional(self, qty: Decimal, price: Decimal) -> bool:
        """True if qty * price meets min_notional."""
        return qty * price >= self.min_notional


class BrokerBase(ABC):
    """Abstract broker interface."""

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> OrderResult: ...

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool: ...

    @abstractmethod
    async def get_order(self, symbol: str, order_id: str) -> OrderResult | None: ...

    @abstractmethod
    async def get_balances(self) -> dict[str, float]:
        """Return {asset: free_balance} mapping."""
        ...

    @abstractmethod
    async def get_symbol_filters(self, symbol: str) -> SymbolFilters | None:
        """Return exchange symbol filters, or None if unavailable."""
        ...
