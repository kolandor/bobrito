"""Trend + Pullback + Momentum Confirmation Strategy (v1).

Long-only intraday strategy:
  1. Confirm uptrend on 5m timeframe  (fast EMA > slow EMA)
  2. Detect pullback on 1m timeframe  (price dipped toward slow EMA)
  3. Confirm momentum resumption      (close crosses back above fast EMA)
  4. Volume confirmation              (current volume > vol_sma × multiplier)
  5. Risk manager approves            (checked externally before order)

Exit signals:
  - Stop loss hit
  - Take profit hit
  - Momentum failure (close < fast EMA on 1m)
"""

from __future__ import annotations

from bobrito.market_data.models import MarketSnapshot
from bobrito.monitoring.logger import get_logger
from bobrito.strategy.base import MarketRegime, Signal, SignalType
from bobrito.strategy.indicators import (
    Indicators,
    is_pullback,
    is_resuming,
    is_uptrend,
)

log = get_logger("strategy.trend_pullback")

# Minimum closed candles required before strategy emits any signal
MIN_1M_CANDLES = 30
MIN_5M_CANDLES = 25


class TrendPullbackStrategy:
    def __init__(
        self,
        ema_fast: int = 9,
        ema_slow: int = 21,
        atr_period: int = 14,
        volume_multiplier: float = 1.5,
        atr_stop_mult: float = 1.5,
        atr_target_mult: float = 3.0,
    ) -> None:
        self._ind = Indicators(ema_fast, ema_slow, atr_period)
        self._vol_mult = volume_multiplier
        self._atr_stop = atr_stop_mult
        self._atr_target = atr_target_mult
        self._last_signal: Signal | None = None

    # ── Public ────────────────────────────────────────────────────────────

    def evaluate(self, snapshot: MarketSnapshot, has_open_position: bool) -> Signal:
        """Return BUY / EXIT / HOLD signal for the current market state."""
        price = snapshot.last_price
        ts = snapshot.timestamp

        if has_open_position:
            return self._evaluate_exit(snapshot)

        # ── Data sufficiency check ─────────────────────────────────────────
        if (
            len(snapshot.candles_1m) < MIN_1M_CANDLES
            or len(snapshot.candles_5m) < MIN_5M_CANDLES
        ):
            return Signal(
                signal_type=SignalType.HOLD,
                symbol=snapshot.symbol,
                price=price,
                timestamp=ts,
                explanation="Insufficient candle history",
            )

        # ── Compute indicators ────────────────────────────────────────────
        ind_5m = self._ind.compute(snapshot.candles_5m)
        ind_1m = self._ind.compute(snapshot.candles_1m)

        # ── Regime detection (5m) ─────────────────────────────────────────
        regime = self._detect_regime(ind_5m)
        if regime == MarketRegime.SIDEWAYS:
            return Signal(
                signal_type=SignalType.HOLD,
                symbol=snapshot.symbol,
                price=price,
                timestamp=ts,
                regime=regime,
                explanation="Sideways market — no trades",
            )

        # ── Entry conditions ──────────────────────────────────────────────
        ema_fast_val = self._ind.last_valid(ind_1m["ema_fast"])
        ema_slow_val = self._ind.last_valid(ind_1m["ema_slow"])
        atr_val = self._ind.last_valid(ind_1m["atr"])

        if ema_fast_val is None or ema_slow_val is None or atr_val is None:
            return Signal(
                signal_type=SignalType.HOLD,
                symbol=snapshot.symbol,
                price=price,
                timestamp=ts,
                explanation="Indicators not ready",
            )

        uptrend_5m = is_uptrend(ind_5m["ema_fast"], ind_5m["ema_slow"])
        pullback_1m = is_pullback(ind_1m["closes"], ind_1m["ema_slow"])
        resuming_1m = is_resuming(ind_1m["closes"], ind_1m["ema_fast"])

        # Volume confirmation
        vol_sma_val = self._ind.last_valid(ind_1m["volume_sma"])
        cur_vol = ind_1m["volumes"][-1] if ind_1m["volumes"] else 0.0
        volume_ok = vol_sma_val is not None and cur_vol >= vol_sma_val * self._vol_mult

        if uptrend_5m and pullback_1m and resuming_1m and volume_ok:
            stop = price - self._atr_stop * atr_val
            target = price + self._atr_target * atr_val
            explanation = (
                f"ENTRY: uptrend_5m={uptrend_5m}, pullback_1m={pullback_1m}, "
                f"resuming={resuming_1m}, vol_ok={volume_ok}, "
                f"ema_fast={ema_fast_val:.2f}, ema_slow={ema_slow_val:.2f}, "
                f"atr={atr_val:.2f}, stop={stop:.2f}, target={target:.2f}"
            )
            sig = Signal(
                signal_type=SignalType.BUY,
                symbol=snapshot.symbol,
                price=price,
                timestamp=ts,
                regime=regime,
                stop_price=stop,
                target_price=target,
                atr=atr_val,
                ema_fast=ema_fast_val,
                ema_slow=ema_slow_val,
                volume_ok=volume_ok,
                explanation=explanation,
            )
            self._last_signal = sig
            log.info(f"BUY signal @ {price:.2f} | {explanation}")
            return sig

        # ── Default: hold ────────────────────────────────────────────────
        reasons = []
        if not uptrend_5m:
            reasons.append("no_uptrend_5m")
        if not pullback_1m:
            reasons.append("no_pullback")
        if not resuming_1m:
            reasons.append("no_resumption")
        if not volume_ok:
            reasons.append("low_volume")

        return Signal(
            signal_type=SignalType.HOLD,
            symbol=snapshot.symbol,
            price=price,
            timestamp=ts,
            regime=regime,
            ema_fast=ema_fast_val,
            ema_slow=ema_slow_val,
            explanation=f"HOLD: {', '.join(reasons)}",
        )

    def _evaluate_exit(self, snapshot: MarketSnapshot) -> Signal:
        """Momentum-failure exit check when a position is open."""
        ind_1m = self._ind.compute(snapshot.candles_1m)
        ema_fast_val = self._ind.last_valid(ind_1m["ema_fast"])
        closes = ind_1m.get("closes", [])

        if ema_fast_val and closes and closes[-1] < ema_fast_val:
            return Signal(
                signal_type=SignalType.EXIT,
                symbol=snapshot.symbol,
                price=snapshot.last_price,
                timestamp=snapshot.timestamp,
                explanation="MOMENTUM FAILURE: close < fast EMA",
            )
        return Signal(
            signal_type=SignalType.HOLD,
            symbol=snapshot.symbol,
            price=snapshot.last_price,
            timestamp=snapshot.timestamp,
            explanation="Position held — momentum intact",
        )

    # ── Helpers ───────────────────────────────────────────────────────────

    def _detect_regime(self, ind_5m: dict) -> MarketRegime:
        """Regime: trending if fast EMA > slow EMA with meaningful separation."""
        ema_fast_val = self._ind.last_valid(ind_5m.get("ema_fast", []))
        ema_slow_val = self._ind.last_valid(ind_5m.get("ema_slow", []))
        if ema_fast_val is None or ema_slow_val is None:
            return MarketRegime.UNKNOWN

        separation_pct = abs(ema_fast_val - ema_slow_val) / ema_slow_val * 100
        if ema_fast_val > ema_slow_val and separation_pct >= 0.05:
            return MarketRegime.TRENDING
        return MarketRegime.SIDEWAYS
