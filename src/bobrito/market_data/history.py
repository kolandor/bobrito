"""Historical candle pre-loader.

Fetches closed OHLCV candles from Binance's public REST API and populates
CandleBuffer instances before the WebSocket feed starts.

Endpoint used (no API key required):
    GET /api/v3/klines?symbol=BTCUSDT&interval=1m&limit=500

Kline response row:
    [open_time_ms, open, high, low, close, volume,
     close_time_ms, quote_volume, num_trades, ...]

Flow on startup:
    1. Fetch `limit` closed 1m candles  → push into buf1
    2. Fetch `limit` closed 5m candles  → push into buf5
    3. Return — WebSocket feed then takes over for live updates

The last candle in each batch is always the currently open candle on
Binance, so we deliberately skip it (is_closed=False would be wrong for
a REST response). We mark all fetched candles as closed=True because
they are historical bars that the exchange has already confirmed.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from bobrito.market_data.buffer import CandleBuffer
from bobrito.market_data.models import Candle
from bobrito.monitoring.logger import get_logger

log = get_logger("market_data.history")

# Public Binance REST base — no key needed for klines
BINANCE_PUBLIC_REST = "https://api.binance.com"
_KLINES_PATH = "/api/v3/klines"

# Maximum candles Binance returns per request
_MAX_LIMIT = 1000


class HistoricalLoader:
    """Downloads and injects historical candles into CandleBuffer instances."""

    def __init__(
        self,
        symbol: str,
        rest_base_url: str = BINANCE_PUBLIC_REST,
        timeout: float = 15.0,
    ) -> None:
        self._symbol = symbol.upper()
        self._base = rest_base_url.rstrip("/")
        self._timeout = timeout

    async def prefill(
        self,
        buf1m: CandleBuffer,
        buf5m: CandleBuffer,
        limit: int = 500,
    ) -> None:
        """Fetch historical candles and fill both buffers.

        Args:
            buf1m:  1-minute CandleBuffer to fill.
            buf5m:  5-minute CandleBuffer to fill.
            limit:  Number of closed candles to fetch per timeframe (max 999).
                    We request limit+1 and drop the last (still-open) bar.
        """
        limit = min(limit, _MAX_LIMIT - 1)  # leave room to drop the open bar

        log.info(
            f"Pre-filling candle buffers | symbol={self._symbol} limit={limit}"
        )

        candles_1m = await self._fetch("1m", limit)
        for c in candles_1m:
            await buf1m.push(c)
        log.info(f"1m buffer pre-filled: {len(buf1m)} closed candles")

        candles_5m = await self._fetch("5m", limit)
        for c in candles_5m:
            await buf5m.push(c)
        log.info(f"5m buffer pre-filled: {len(buf5m)} closed candles")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=5))
    async def _fetch(self, interval: str, limit: int) -> list[Candle]:
        """Fetch `limit` closed candles for the given interval.

        Requests limit+1 bars and discards the last one, which is the
        currently open (incomplete) candle on Binance.
        """
        params = {
            "symbol": self._symbol,
            "interval": interval,
            "limit": limit + 1,   # +1 so we can drop the open bar
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{self._base}{_KLINES_PATH}", params=params)
            resp.raise_for_status()
            rows: list[list] = resp.json()

        if not rows:
            log.warning(f"No kline data returned for {interval}")
            return []

        # Drop the last row — it is the currently building (open) candle
        closed_rows = rows[:-1]

        candles = [_row_to_candle(row, interval) for row in closed_rows]
        log.debug(
            f"Fetched {len(candles)} closed {interval} candles "
            f"from {candles[0].open_time} to {candles[-1].open_time}"
        )
        return candles


def _row_to_candle(row: list, interval: str) -> Candle:
    """Convert a single Binance kline REST row to a Candle dataclass."""
    return Candle(
        open_time=datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc).replace(tzinfo=None),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
        interval=interval,
        is_closed=True,
        num_trades=int(row[8]),
    )
