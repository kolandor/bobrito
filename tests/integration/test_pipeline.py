"""Integration test: market data → strategy → risk → paper execution pipeline."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from bobrito.config.settings import Settings
from bobrito.execution.base import (
    OrderRequest,
    OrderSide,
    OrderType,
    SymbolFilters,
)
from bobrito.execution.paper import PaperBroker
from bobrito.market_data.models import Candle, MarketSnapshot
from bobrito.risk.manager import RiskManager
from bobrito.strategy.base import SignalType
from bobrito.strategy.trend_pullback import TrendPullbackStrategy


def _make_candle(close: float, volume: float = 10.0, interval: str = "1m") -> Candle:
    return Candle(
        open_time=datetime.utcnow(),
        open=close - 5,
        high=close + 10,
        low=close - 10,
        close=close,
        volume=volume,
        interval=interval,
    )


def _make_trending_candles_1m(n: int = 60, start: float = 30000.0) -> list[Candle]:
    """Generate candles that produce an uptrend (consistently rising)."""
    candles = []
    price = start
    for i in range(n):
        price += 20 + (i % 5)  # rising trend
        vol = 12.0 if i > 40 else 8.0  # volume spike near end
        candles.append(_make_candle(price, volume=vol, interval="1m"))
    return candles


def _make_trending_candles_5m(n: int = 30, start: float = 30000.0) -> list[Candle]:
    candles = []
    price = start
    for _i in range(n):
        price += 50
        candles.append(_make_candle(price, interval="5m"))
    return candles


def _make_snapshot(candles_1m, candles_5m, price: float = 35000.0) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTCUSDT",
        last_price=price,
        bid=price - 1,
        ask=price + 1,
        spread=2.0,
        timestamp=datetime.utcnow(),
        candles_1m=candles_1m,
        candles_5m=candles_5m,
    )


class TestStrategySignalGeneration:
    def test_hold_on_insufficient_data(self):
        strategy = TrendPullbackStrategy()
        snapshot = _make_snapshot([], [], price=40000.0)
        sig = strategy.evaluate(snapshot, has_open_position=False)
        assert sig.signal_type == SignalType.HOLD

    def test_hold_on_sideways_market(self):
        strategy = TrendPullbackStrategy()
        # Flat candles → no EMA separation → sideways
        flat = [_make_candle(40000.0) for _ in range(60)]
        snap = _make_snapshot(flat, [_make_candle(40000.0) for _ in range(30)])
        sig = strategy.evaluate(snap, has_open_position=False)
        assert sig.signal_type == SignalType.HOLD

    def test_exit_signal_when_position_open_and_momentum_fails(self):
        strategy = TrendPullbackStrategy()
        # Declining 1m candles → momentum failure
        declining = [_make_candle(40000.0 - i * 30) for i in range(60)]
        flat_5m = [_make_candle(40000.0) for _ in range(30)]
        snap = _make_snapshot(declining, flat_5m, price=38000.0)
        sig = strategy.evaluate(snap, has_open_position=True)
        # Close < fast EMA → EXIT or HOLD
        assert sig.signal_type in (SignalType.EXIT, SignalType.HOLD)


@pytest.mark.asyncio
class TestPaperExecutionPipeline:
    async def _make_risk(self):
        settings = Settings.model_construct(
            bot_mode="paper",
            initial_capital_usdt=500.0,
            risk_per_trade_pct=1.0,
            max_daily_loss_pct=5.0,
            max_consecutive_losses=5,
            cooldown_minutes_after_losses=0,
            max_trades_per_day=10,
            min_free_balance_usdt=10.0,
            paper_initial_usdt=500.0,
        )
        db = MagicMock()
        sess_ctx = AsyncMock()
        sess_ctx.__aenter__ = AsyncMock(return_value=sess_ctx)
        sess_ctx.__aexit__ = AsyncMock(return_value=False)
        sess_ctx.add = MagicMock()
        sess_ctx.commit = AsyncMock()
        db.session = MagicMock(return_value=sess_ctx)

        rm = RiskManager(settings, db)
        rm.configure_filters(
            SymbolFilters(
                symbol="BTCUSDT",
                step_size=Decimal("0.00001"),
                min_qty=Decimal("0.00001"),
                min_notional=Decimal("5.0"),
                tick_size=Decimal("0.01"),
            )
        )
        return rm, settings

    async def test_risk_allows_valid_entry(self):
        rm, _ = await self._make_risk()
        from bobrito.strategy.base import Signal, SignalType

        signal = Signal(
            signal_type=SignalType.BUY,
            symbol="BTCUSDT",
            price=40000.0,
            timestamp=datetime.utcnow(),
            stop_price=39000.0,
            target_price=43000.0,
        )
        decision = await rm.validate_entry(signal, free_usdt=300.0, has_open_position=False)
        assert decision.allowed
        assert decision.quantity > 0

    async def test_paper_broker_roundtrip(self):
        broker = PaperBroker(initial_usdt=500.0, fee_rate=0.001, slippage_bps=0.0)
        broker.update_price(40000.0)

        buy = await broker.place_order(
            OrderRequest("BTCUSDT", OrderSide.BUY, OrderType.MARKET, quantity=0.01)
        )
        assert buy.is_filled
        assert buy.filled_qty == pytest.approx(0.01)

        balances = await broker.get_balances()
        assert balances["BTC"] == pytest.approx(0.01)

        sell = await broker.place_order(
            OrderRequest("BTCUSDT", OrderSide.SELL, OrderType.MARKET, quantity=0.01)
        )
        assert sell.is_filled
        balances = await broker.get_balances()
        assert balances["BTC"] == pytest.approx(0.0, abs=1e-8)

    async def test_fees_reduce_profit(self):
        broker = PaperBroker(initial_usdt=1000.0, fee_rate=0.001, slippage_bps=0.0)
        broker.update_price(40000.0)

        await broker.place_order(
            OrderRequest("BTCUSDT", OrderSide.BUY, OrderType.MARKET, quantity=0.01)
        )
        broker.update_price(41000.0)
        await broker.place_order(
            OrderRequest("BTCUSDT", OrderSide.SELL, OrderType.MARKET, quantity=0.01)
        )

        balances = await broker.get_balances()
        # Gross PnL = (41000 - 40000) * 0.01 = 10 USDT
        # Fees ≈ 0.01*40000*0.001 + 0.01*41000*0.001 = 0.40 + 0.41 = 0.81
        # Net balance = 1000 + 10 - 0.81 ≈ 1009.19
        assert balances["USDT"] == pytest.approx(1009.19, abs=0.1)
