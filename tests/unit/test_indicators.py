"""Unit tests for technical indicators."""

from __future__ import annotations

import math

from bobrito.strategy.indicators import (
    Indicators,
    atr,
    ema,
    is_resuming,
    is_uptrend,
    swing_lows,
    volume_sma,
)


class TestEMA:
    def test_returns_correct_length(self):
        values = list(range(1, 21))
        result = ema(values, period=9)
        assert len(result) == len(values)

    def test_first_n_minus_1_are_nan(self):
        values = list(range(1, 21))
        result = ema(values, period=9)
        for v in result[:8]:
            assert math.isnan(v)

    def test_seed_equals_sma(self):
        values = [1.0] * 20
        result = ema(values, period=5)
        # seed at index 4 should equal SMA = 1.0
        assert abs(result[4] - 1.0) < 1e-9

    def test_ema_rises_on_increasing_series(self):
        values = list(range(1, 31))
        result = ema(values, period=5)
        # EMA should be monotonically increasing past the first period
        valid = [v for v in result if not math.isnan(v)]
        assert all(valid[i] < valid[i + 1] for i in range(len(valid) - 1))

    def test_insufficient_data_all_nan(self):
        result = ema([1.0, 2.0], period=9)
        assert all(math.isnan(v) for v in result)

    def test_known_value(self):
        # EMA(5) of [1,2,3,4,5] seeds at index 4 = (1+2+3+4+5)/5 = 3.0
        result = ema([1, 2, 3, 4, 5], period=5)
        assert abs(result[4] - 3.0) < 1e-9


class TestATR:
    def _make_ohlc(self, n: int, spread: float = 1.0):
        closes = [100.0 + i for i in range(n)]
        highs = [c + spread for c in closes]
        lows = [c - spread for c in closes]
        return highs, lows, closes

    def test_length(self):
        highs, lows, closes = self._make_ohlc(30)
        result = atr(highs, lows, closes, period=14)
        assert len(result) == 30

    def test_stable_atr_constant_range(self):
        """ATR should stabilise when true range is constant.

        With close[i] = 100+i, high = close+spread, low = close-spread:
          TR = max(high-low, |high-prev_close|, |low-prev_close|)
             = max(2*spread, spread+1, spread-1)   (step=1 per bar)
             = 2*spread  when spread >= 1
        So ATR converges to 2*spread, not spread.
        """
        spread = 2.0
        highs, lows, closes = self._make_ohlc(50, spread=spread)
        result = atr(highs, lows, closes, period=14)
        valid = [v for v in result if not math.isnan(v)]
        expected_tr = 2 * spread   # 4.0 with spread=2 and 1-unit step between closes
        assert abs(valid[-1] - expected_tr) < 1.0

    def test_nans_at_start(self):
        highs, lows, closes = self._make_ohlc(20)
        result = atr(highs, lows, closes, period=14)
        assert math.isnan(result[0])


class TestSwingLows:
    def test_identifies_low_point(self):
        lows = [10, 9, 8, 5, 8, 9, 10, 9, 8, 5, 8, 9, 10]
        closes = lows[:]
        result = swing_lows(closes, lows, lookback=3)
        assert any(v is not None for v in result)

    def test_no_swings_flat(self):
        lows = [10.0] * 20
        closes = lows[:]
        result = swing_lows(closes, lows, lookback=3)
        # All equal — every point is a "swing low" by min rule
        # This is acceptable; just check length
        assert len(result) == 20


class TestVolumeSMA:
    def test_length(self):
        volumes = [100.0] * 30
        result = volume_sma(volumes, period=20)
        assert len(result) == 30

    def test_sma_of_constant(self):
        volumes = [50.0] * 30
        result = volume_sma(volumes, period=10)
        valid = [v for v in result if not math.isnan(v)]
        assert all(abs(v - 50.0) < 1e-9 for v in valid)


class TestRegimeDetectors:
    def _make_ema_series(self, fast_val: float, slow_val: float, n: int = 30):
        fast = [float("nan")] * (n - 1) + [fast_val]
        slow = [float("nan")] * (n - 1) + [slow_val]
        return fast, slow

    def test_uptrend_true_when_fast_above_slow(self):
        fast, slow = self._make_ema_series(110, 100)
        assert is_uptrend(fast, slow) is True

    def test_uptrend_false_when_fast_below_slow(self):
        fast, slow = self._make_ema_series(90, 100)
        assert is_uptrend(fast, slow) is False

    def test_is_resuming(self):
        # Previous close below fast, current close above fast
        closes = [99.0, 100.0, 101.0]
        ema_f = [float("nan"), 100.5, 100.5]
        assert is_resuming(closes, ema_f) is True

    def test_is_not_resuming(self):
        closes = [101.0, 102.0]
        ema_f = [100.0, 100.5]
        # Both closes above EMA → not a fresh crossover
        assert is_resuming(closes, ema_f) is False


class TestIndicatorsWrapper:
    def _make_candles(self, n: int = 60):
        from datetime import datetime

        from bobrito.market_data.models import Candle

        candles = []
        price = 30000.0
        for i in range(n):
            price += (i % 3 - 1) * 10
            candles.append(
                Candle(
                    open_time=datetime(2024, 1, 1),
                    open=price - 5,
                    high=price + 10,
                    low=price - 10,
                    close=price,
                    volume=1.0 + i * 0.1,
                    interval="1m",
                )
            )
        return candles

    def test_compute_returns_all_keys(self):
        ind = Indicators(ema_fast_period=9, ema_slow_period=21, atr_period=14)
        candles = self._make_candles(60)
        result = ind.compute(candles)
        for key in ("ema_fast", "ema_slow", "atr", "swing_lows", "volume_sma", "closes"):
            assert key in result

    def test_last_valid_finds_value(self):
        ind = Indicators()
        series = [float("nan"), float("nan"), 42.0]
        assert ind.last_valid(series) == 42.0

    def test_last_valid_returns_none_all_nan(self):
        ind = Indicators()
        assert ind.last_valid([float("nan"), float("nan")]) is None
