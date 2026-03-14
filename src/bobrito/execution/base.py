"""Unified broker interface.

All broker implementations must subclass BrokerBase so that the strategy
and bot engine remain broker-agnostic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
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
    price: float | None = None        # Limit price (None = market)
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


class BrokerBase(ABC):
    """Abstract broker interface."""

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> OrderResult:
        ...

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        ...

    @abstractmethod
    async def get_order(self, symbol: str, order_id: str) -> OrderResult | None:
        ...

    @abstractmethod
    async def get_balances(self) -> dict[str, float]:
        """Return {asset: free_balance} mapping."""
        ...

    @abstractmethod
    async def get_symbol_filters(self, symbol: str) -> dict:
        """Return exchange filter info: step_size, min_qty, min_notional."""
        ...
