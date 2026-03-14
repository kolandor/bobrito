"""Thread-safe ring-buffer for OHLCV candles."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from datetime import datetime

from bobrito.market_data.models import Candle


class CandleBuffer:
    """Fixed-size FIFO buffer for closed candles of a given interval."""

    def __init__(self, interval: str, maxlen: int = 500) -> None:
        self.interval = interval
        self._buf: deque[Candle] = deque(maxlen=maxlen)
        self._lock = asyncio.Lock()
        self._callbacks: list[Callable[[Candle], None]] = []
        # Current building candle (may be open)
        self._current: Candle | None = None

    def add_callback(self, cb: Callable[[Candle], None]) -> None:
        self._callbacks.append(cb)

    async def push(self, candle: Candle) -> None:
        """Push a candle into the buffer.

        If the candle is open (is_closed=False) it updates _current only.
        If the candle is closed it is appended to the deque.
        """
        async with self._lock:
            if not candle.is_closed:
                self._current = candle
                return
            self._current = candle
            self._buf.append(candle)

        for cb in self._callbacks:
            cb(candle)

    def update_from_kline_event(self, event: dict) -> Candle:
        """Parse a Binance kline WebSocket event dict and return a Candle."""
        k = event["k"]
        candle = Candle(
            open_time=datetime.fromtimestamp(k["t"] / 1000),
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
            interval=k["i"],
            is_closed=bool(k["x"]),
            num_trades=int(k["n"]),
        )
        return candle

    def candles(self) -> list[Candle]:
        """Return list of closed candles (oldest first)."""
        return list(self._buf)

    def latest(self, n: int = 1) -> list[Candle]:
        """Return the most recent n closed candles."""
        buf = list(self._buf)
        return buf[-n:]

    def current(self) -> Candle | None:
        return self._current

    def __len__(self) -> int:
        return len(self._buf)

    def is_ready(self, min_candles: int = 50) -> bool:
        return len(self._buf) >= min_candles
