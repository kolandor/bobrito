"""Pure-function technical indicators.

All functions operate on plain Python lists or numpy arrays and have
no side-effects, making them trivially unit-testable.
"""

from __future__ import annotations

import numpy as np


def ema(values: list[float], period: int) -> list[float]:
    """Exponential Moving Average.

    Returns a list of the same length as `values` where the first
    `period-1` elements are NaN (insufficient data).
    """
    if len(values) < period:
        return [float("nan")] * len(values)

    result: list[float] = [float("nan")] * len(values)
    k = 2.0 / (period + 1)
    # Seed with SMA of first `period` bars
    seed = sum(values[:period]) / period
    result[period - 1] = seed
    for i in range(period, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    return result


def atr(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> list[float]:
    """Average True Range."""
    n = len(closes)
    if n < 2:
        return [float("nan")] * n

    tr_values: list[float] = [float("nan")]
    for i in range(1, n):
        h_l = highs[i] - lows[i]
        h_pc = abs(highs[i] - closes[i - 1])
        l_pc = abs(lows[i] - closes[i - 1])
        tr_values.append(max(h_l, h_pc, l_pc))

    # Wilder's smoothing (same as RMA / EMA with alpha=1/period)
    result: list[float] = [float("nan")] * n
    if n > period:
        seed = sum(tr_values[1 : period + 1]) / period
        result[period] = seed
        for i in range(period + 1, n):
            result[i] = (result[i - 1] * (period - 1) + tr_values[i]) / period
    return result


def swing_lows(closes: list[float], lows: list[float], lookback: int = 5) -> list[float | None]:
    """Identify swing-low pivot prices.

    A bar at index i is a swing low when its low is lower than the
    `lookback` bars on each side. Returns a list where non-None entries
    mark confirmed swing-low prices.
    """
    n = len(closes)
    result: list[float | None] = [None] * n
    for i in range(lookback, n - lookback):
        window = lows[i - lookback : i + lookback + 1]
        if lows[i] == min(window):
            result[i] = lows[i]
    return result


def volume_sma(volumes: list[float], period: int) -> list[float]:
    """Simple Moving Average of volume."""
    n = len(volumes)
    result: list[float] = [float("nan")] * n
    for i in range(period - 1, n):
        result[i] = sum(volumes[i - period + 1 : i + 1]) / period
    return result


def is_uptrend(ema_fast_series: list[float], ema_slow_series: list[float]) -> bool:
    """True when the most recent valid fast EMA is above the slow EMA."""
    fast = next((v for v in reversed(ema_fast_series) if not _isnan(v)), None)
    slow = next((v for v in reversed(ema_slow_series) if not _isnan(v)), None)
    if fast is None or slow is None:
        return False
    return fast > slow


def is_pullback(
    closes: list[float],
    ema_slow_series: list[float],
    lookback: int = 5,
    near_pct: float = 0.2,
) -> bool:
    """True when price has recently dipped toward / near the slow EMA.

    We look back `lookback` bars and check whether any close was within
    `near_pct`% of the slow EMA (e.g. near_pct=0.2 → close ≤ slow_ema * 1.002).
    """
    if len(closes) < lookback + 1:
        return False
    recent = closes[-(lookback + 1) : -1]
    slow_val = next((v for v in reversed(ema_slow_series) if not _isnan(v)), None)
    if slow_val is None:
        return False
    threshold = slow_val * (1 + near_pct / 100)
    return any(c <= threshold for c in recent)


def is_resuming(closes: list[float], ema_fast_series: list[float]) -> bool:
    """True when the latest close has crossed back above the fast EMA."""
    if len(closes) < 2:
        return False
    fast = next((v for v in reversed(ema_fast_series) if not _isnan(v)), None)
    if fast is None:
        return False
    return closes[-1] > fast and closes[-2] <= fast


def _isnan(v: float) -> bool:
    try:
        return np.isnan(v)
    except (TypeError, ValueError):
        return True


class Indicators:
    """Convenience wrapper: compute all indicators from candle lists."""

    def __init__(
        self,
        ema_fast_period: int = 9,
        ema_slow_period: int = 21,
        atr_period: int = 14,
        volume_sma_period: int = 20,
        swing_low_lookback: int = 5,
    ):
        self.ema_fast_period = ema_fast_period
        self.ema_slow_period = ema_slow_period
        self.atr_period = atr_period
        self.volume_sma_period = volume_sma_period
        self.swing_low_lookback = swing_low_lookback

    def compute(self, candles: list) -> dict:
        """
        Args:
            candles: list of Candle objects (oldest first).
        Returns:
            dict with keys: ema_fast, ema_slow, atr, swing_lows, volume_sma,
                            closes, highs, lows, volumes.
        """
        if not candles:
            return {}

        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        volumes = [c.volume for c in candles]

        ema_fast_series = ema(closes, self.ema_fast_period)
        ema_slow_series = ema(closes, self.ema_slow_period)
        atr_series = atr(highs, lows, closes, self.atr_period)
        swings = swing_lows(closes, lows, self.swing_low_lookback)
        vol_sma = volume_sma(volumes, self.volume_sma_period)

        return {
            "closes": closes,
            "highs": highs,
            "lows": lows,
            "volumes": volumes,
            "ema_fast": ema_fast_series,
            "ema_slow": ema_slow_series,
            "atr": atr_series,
            "swing_lows": swings,
            "volume_sma": vol_sma,
        }

    @staticmethod
    def last_valid(series: list[float]) -> float | None:
        """Return most recent non-NaN value in a series."""
        for v in reversed(series):
            if not _isnan(v):
                return v
        return None
