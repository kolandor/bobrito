"""Unit tests for PaperBroker."""

from __future__ import annotations

import pytest

from bobrito.execution.base import OrderRequest, OrderSide, OrderStatus, OrderType
from bobrito.execution.paper import PaperBroker


@pytest.fixture
def broker() -> PaperBroker:
    return PaperBroker(initial_usdt=500.0, fee_rate=0.001, slippage_bps=0.0)


class TestPaperBrokerBalance:
    @pytest.mark.asyncio
    async def test_initial_balance(self, broker: PaperBroker):
        balances = await broker.get_balances()
        assert balances["USDT"] == pytest.approx(500.0)
        assert balances["BTC"] == pytest.approx(0.0)


class TestPaperBrokerBuyOrder:
    @pytest.mark.asyncio
    async def test_buy_debits_usdt(self, broker: PaperBroker):
        broker.update_price(40000.0)
        req = OrderRequest(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=0.001,
        )
        result = await broker.place_order(req)
        assert result.status == OrderStatus.FILLED
        assert result.filled_qty == pytest.approx(0.001)
        balances = await broker.get_balances()
        # Should have BTC now
        assert balances["BTC"] == pytest.approx(0.001)
        # USDT reduced by notional + fee
        expected_notional = 0.001 * 40000.0
        expected_fee = expected_notional * 0.001
        assert balances["USDT"] == pytest.approx(500.0 - expected_notional - expected_fee)

    @pytest.mark.asyncio
    async def test_sell_credits_usdt(self, broker: PaperBroker):
        broker.update_price(40000.0)
        # First buy
        await broker.place_order(
            OrderRequest("BTCUSDT", OrderSide.BUY, OrderType.MARKET, 0.001)
        )
        # Then sell
        result = await broker.place_order(
            OrderRequest("BTCUSDT", OrderSide.SELL, OrderType.MARKET, 0.001)
        )
        assert result.status == OrderStatus.FILLED
        balances = await broker.get_balances()
        assert balances["BTC"] == pytest.approx(0.0, abs=1e-8)

    @pytest.mark.asyncio
    async def test_commission_charged(self, broker: PaperBroker):
        broker.update_price(40000.0)
        req = OrderRequest("BTCUSDT", OrderSide.BUY, OrderType.MARKET, 0.001)
        result = await broker.place_order(req)
        expected_commission = 0.001 * 40000.0 * 0.001
        assert result.commission == pytest.approx(expected_commission)


class TestPaperBrokerSlippage:
    @pytest.mark.asyncio
    async def test_buy_slippage_increases_price(self):
        broker = PaperBroker(initial_usdt=500.0, fee_rate=0.0, slippage_bps=10.0)
        broker.update_price(40000.0)
        req = OrderRequest("BTCUSDT", OrderSide.BUY, OrderType.MARKET, 0.001)
        result = await broker.place_order(req)
        expected = 40000.0 * (1 + 10 / 10_000)
        assert result.average_price == pytest.approx(expected)

    @pytest.mark.asyncio
    async def test_sell_slippage_decreases_price(self):
        broker = PaperBroker(initial_usdt=500.0, fee_rate=0.0, slippage_bps=10.0)
        broker.update_price(40000.0)
        # Seed BTC manually
        broker._balances["BTC"]["free"] = 0.001
        req = OrderRequest("BTCUSDT", OrderSide.SELL, OrderType.MARKET, 0.001)
        result = await broker.place_order(req)
        expected = 40000.0 * (1 - 10 / 10_000)
        assert result.average_price == pytest.approx(expected)


class TestPaperBrokerFilters:
    @pytest.mark.asyncio
    async def test_get_symbol_filters_returns_none_when_not_set(self, broker: PaperBroker):
        filters = await broker.get_symbol_filters("BTCUSDT")
        assert filters is None

    @pytest.mark.asyncio
    async def test_get_symbol_filters_returns_filters_when_set(self, broker: PaperBroker):
        from decimal import Decimal

        from bobrito.execution.base import SymbolFilters

        f = SymbolFilters(
            symbol="BTCUSDT",
            step_size=Decimal("0.00001"),
            min_qty=Decimal("0.00001"),
            min_notional=Decimal("5.0"),
            tick_size=Decimal("0.01"),
        )
        broker.set_filters(f)
        filters = await broker.get_symbol_filters("BTCUSDT")
        assert filters is not None
        assert filters.step_size == Decimal("0.00001")
        assert filters.min_qty == Decimal("0.00001")
        assert filters.min_notional == Decimal("5.0")


class TestPaperBrokerCancelOrder:
    @pytest.mark.asyncio
    async def test_cancel_existing_order(self, broker: PaperBroker):
        broker.update_price(40000.0)
        req = OrderRequest(
            "BTCUSDT", OrderSide.BUY, OrderType.MARKET, 0.001, client_order_id="test-1"
        )
        await broker.place_order(req)
        cancelled = await broker.cancel_order("BTCUSDT", "test-1")
        assert cancelled is True

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_order(self, broker: PaperBroker):
        cancelled = await broker.cancel_order("BTCUSDT", "does-not-exist")
        assert cancelled is False
