"""Unit tests for CandleBuffer."""

from __future__ import annotations

from datetime import datetime

import pytest

from bobrito.market_data.buffer import CandleBuffer
from bobrito.market_data.models import Candle


def make_candle(close: float, is_closed: bool = True) -> Candle:
    return Candle(
        open_time=datetime.utcnow(),
        open=close - 1,
        high=close + 1,
        low=close - 2,
        close=close,
        volume=10.0,
        interval="1m",
        is_closed=is_closed,
    )


class TestCandleBuffer:
    @pytest.mark.asyncio
    async def test_push_closed_candle_stored(self):
        buf = CandleBuffer("1m", maxlen=10)
        await buf.push(make_candle(100.0, is_closed=True))
        assert len(buf) == 1

    @pytest.mark.asyncio
    async def test_push_open_candle_not_stored(self):
        buf = CandleBuffer("1m", maxlen=10)
        await buf.push(make_candle(100.0, is_closed=False))
        assert len(buf) == 0
        assert buf.current() is not None

    @pytest.mark.asyncio
    async def test_maxlen_respected(self):
        buf = CandleBuffer("1m", maxlen=5)
        for i in range(10):
            await buf.push(make_candle(float(i), is_closed=True))
        assert len(buf) == 5

    @pytest.mark.asyncio
    async def test_latest_returns_most_recent(self):
        buf = CandleBuffer("1m", maxlen=10)
        for i in range(5):
            await buf.push(make_candle(float(i), is_closed=True))
        latest = buf.latest(1)
        assert len(latest) == 1
        assert latest[0].close == pytest.approx(4.0)

    @pytest.mark.asyncio
    async def test_is_ready(self):
        buf = CandleBuffer("1m", maxlen=100)
        assert not buf.is_ready(min_candles=30)
        for i in range(30):
            await buf.push(make_candle(float(i)))
        assert buf.is_ready(min_candles=30)

    @pytest.mark.asyncio
    async def test_callback_fired_on_closed(self):
        buf = CandleBuffer("1m", maxlen=10)
        received = []
        buf.add_callback(received.append)
        await buf.push(make_candle(100.0, is_closed=True))
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_callback_not_fired_on_open(self):
        buf = CandleBuffer("1m", maxlen=10)
        received = []
        buf.add_callback(received.append)
        await buf.push(make_candle(100.0, is_closed=False))
        assert len(received) == 0
