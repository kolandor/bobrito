"""Binance WebSocket market data feed with reconnect logic.

Subscribes to:
  - <symbol>@kline_1m
  - <symbol>@kline_5m
  - <symbol>@bookTicker  (best bid/ask)
  - <symbol>@trade       (tick stream)

On each new closed candle the feed triggers a MarketSnapshot callback
so downstream consumers (strategy) receive a fully assembled view.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Coroutine
from datetime import datetime

import websockets

from bobrito.market_data.buffer import CandleBuffer
from bobrito.market_data.models import Candle, MarketSnapshot
from bobrito.monitoring.logger import get_logger
from bobrito.monitoring.metrics import MetricsCollector

log = get_logger("market_data.feed")

SnapshotCallback = Callable[[MarketSnapshot], Coroutine]


class MarketDataFeed:
    """Manages WebSocket connections and feeds normalised data to callbacks."""

    def __init__(
        self,
        symbol: str,
        ws_base_url: str,
        buffer_1m: CandleBuffer,
        buffer_5m: CandleBuffer,
        on_snapshot: SnapshotCallback,
    ) -> None:
        self._symbol = symbol.lower()
        self._ws_base = ws_base_url.rstrip("/")
        self._buf1 = buffer_1m
        self._buf5 = buffer_5m
        self._on_snapshot = on_snapshot

        self._last_price: float = 0.0
        self._best_bid: float = 0.0
        self._best_ask: float = 0.0
        self._last_feed_ts: float = time.time()

        self._running = False
        self._task: asyncio.Task | None = None

    # ── Public ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_forever())
        log.info(f"MarketDataFeed started for {self._symbol.upper()}")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("MarketDataFeed stopped")

    # ── Internals ─────────────────────────────────────────────────────────

    async def _run_forever(self) -> None:
        streams = [
            f"{self._symbol}@kline_1m",
            f"{self._symbol}@kline_5m",
            f"{self._symbol}@bookTicker",
            f"{self._symbol}@trade",
        ]
        stream_path = "/".join(streams)
        url = f"{self._ws_base}/stream?streams={stream_path}"

        while self._running:
            try:
                await self._connect(url)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                MetricsCollector.ws_connected.set(0)
                log.warning(f"Feed disconnected: {exc}. Reconnecting in 5 s…")
                await asyncio.sleep(5)

    async def _connect(self, url: str) -> None:
        log.info(f"Connecting to {url}")
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            MetricsCollector.ws_connected.set(1)
            log.info("WebSocket connected")
            async for raw in ws:
                if not self._running:
                    break
                await self._dispatch(raw)

    async def _dispatch(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        data = msg.get("data", msg)
        event_type = data.get("e")

        if event_type == "kline":
            await self._handle_kline(data)
        elif event_type == "bookTicker":
            self._handle_book_ticker(data)
        elif event_type == "trade":
            self._handle_trade(data)

        self._last_feed_ts = time.time()
        MetricsCollector.candle_feed_lag_seconds.set(0)

    async def _handle_kline(self, data: dict) -> None:
        k = data["k"]
        interval = k["i"]
        candle = Candle(
            open_time=datetime.fromtimestamp(k["t"] / 1000),
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
            interval=interval,
            is_closed=bool(k["x"]),
            num_trades=int(k["n"]),
        )
        self._last_price = candle.close

        if interval == "1m":
            await self._buf1.push(candle)
        elif interval == "5m":
            await self._buf5.push(candle)

        if candle.is_closed and interval == "1m":
            snapshot = self._build_snapshot()
            await self._on_snapshot(snapshot)

    def _handle_book_ticker(self, data: dict) -> None:
        self._best_bid = float(data.get("b", self._best_bid))
        self._best_ask = float(data.get("a", self._best_ask))

    def _handle_trade(self, data: dict) -> None:
        self._last_price = float(data["p"])

    def _build_snapshot(self) -> MarketSnapshot:
        bid = self._best_bid or self._last_price
        ask = self._best_ask or self._last_price
        spread = ask - bid if ask and bid else 0.0
        return MarketSnapshot(
            symbol=self._symbol.upper(),
            last_price=self._last_price,
            bid=bid,
            ask=ask,
            spread=spread,
            timestamp=datetime.utcnow(),
            candles_1m=self._buf1.candles(),
            candles_5m=self._buf5.candles(),
            current_1m=self._buf1.current(),
            current_5m=self._buf5.current(),
        )

    def build_snapshot(self) -> MarketSnapshot:
        """Exposed for external callers (e.g. API status endpoint)."""
        return self._build_snapshot()

    @property
    def feed_lag_seconds(self) -> float:
        return time.time() - self._last_feed_ts
