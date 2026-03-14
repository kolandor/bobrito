"""Common value objects for strategy signals."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class SignalType(str, Enum):
    BUY = "BUY"
    EXIT = "EXIT"
    HOLD = "HOLD"


class MarketRegime(str, Enum):
    TRENDING = "TRENDING"
    SIDEWAYS = "SIDEWAYS"
    UNKNOWN = "UNKNOWN"


@dataclass
class Signal:
    signal_type: SignalType
    symbol: str
    price: float
    timestamp: datetime
    regime: MarketRegime = MarketRegime.UNKNOWN
    stop_price: float | None = None
    target_price: float | None = None
    atr: float | None = None
    ema_fast: float | None = None
    ema_slow: float | None = None
    volume_ok: bool = False
    explanation: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def is_actionable(self) -> bool:
        return self.signal_type in (SignalType.BUY, SignalType.EXIT)
