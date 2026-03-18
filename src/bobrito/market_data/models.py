"""Immutable value objects for normalised market data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Candle:
    """A single OHLCV candle."""

    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    interval: str  # e.g. "1m", "5m"
    is_closed: bool = True
    num_trades: int = 0

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open


@dataclass(frozen=True, slots=True)
class Trade:
    """An individual trade / tick from the exchange trade stream."""

    trade_id: int
    price: float
    quantity: float
    timestamp: datetime
    is_buyer_maker: bool


@dataclass
class MarketSnapshot:
    """Aggregated view of current market state passed to the strategy."""

    symbol: str
    last_price: float
    bid: float
    ask: float
    spread: float
    timestamp: datetime
    # Candle buffers (closed candles only)
    candles_1m: list[Candle] = field(default_factory=list)
    candles_5m: list[Candle] = field(default_factory=list)
    # Current (possibly open) candle
    current_1m: Candle | None = None
    current_5m: Candle | None = None

    @property
    def mid_price(self) -> float:
        return (self.bid + self.ask) / 2.0

    def spread_bps(self) -> float:
        if self.mid_price == 0:
            return 0.0
        return (self.spread / self.mid_price) * 10_000
