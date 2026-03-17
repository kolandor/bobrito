"""Unit tests for SymbolFilters: parsing, quantize_qty/price, validation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from bobrito.execution.base import SymbolFilters


def make_filters(
    symbol: str = "BTCUSDT",
    step_size: Decimal | float = 0.00001,
    min_qty: Decimal | float = 0.00001,
    min_notional: Decimal | float = 5.0,
    tick_size: Decimal | float = 0.01,
) -> SymbolFilters:
    return SymbolFilters(
        symbol=symbol,
        step_size=Decimal(str(step_size)),
        min_qty=Decimal(str(min_qty)),
        min_notional=Decimal(str(min_notional)),
        tick_size=Decimal(str(tick_size)),
    )


class TestQuantizeQty:
    def test_rounds_down_to_step(self):
        f = make_filters(step_size=0.00001)
        assert f.quantize_qty(0.123456789) == Decimal("0.12345")

    def test_handles_larger_step(self):
        f = make_filters(step_size=0.001)
        assert f.quantize_qty(1.2345) == Decimal("1.234")

    def test_min_qty_preserved(self):
        f = make_filters(step_size=0.00001, min_qty=0.00001)
        assert f.quantize_qty(0.00001) == Decimal("0.00001")


class TestQuantizePrice:
    def test_rounds_to_tick(self):
        f = make_filters(tick_size=0.01)
        assert f.quantize_price(40001.234) == Decimal("40001.23")

    def test_handles_small_tick(self):
        f = make_filters(tick_size=0.00001)
        assert f.quantize_price(40000.123456) == Decimal("40000.12345")

    def test_tick_size_quantization_down(self):
        """Price quantization uses ROUND_DOWN to tick_size precision."""
        f = make_filters(tick_size=0.1)
        assert f.quantize_price(40000.99) == Decimal("40000.9")

    def test_tick_size_quantization_decimal_input(self):
        """quantize_price accepts Decimal input without float precision loss."""
        f = make_filters(tick_size=0.01)
        result = f.quantize_price(Decimal("40001.235"))
        assert result == Decimal("40001.23")


class TestCheckQty:
    def test_valid_qty_passes(self):
        f = make_filters(step_size=0.00001, min_qty=0.00001)
        assert f.check_qty(Decimal("0.01")) is True

    def test_below_min_fails(self):
        f = make_filters(min_qty=0.001)
        assert f.check_qty(Decimal("0.0001")) is False

    def test_misaligned_step_fails(self):
        f = make_filters(step_size=0.001, min_qty=0.001)
        # 0.0015 is not aligned to step 0.001 (would quantize to 0.001)
        assert f.check_qty(Decimal("0.0015")) is False


class TestCheckNotional:
    def test_sufficient_notional_passes(self):
        f = make_filters(min_notional=5.0)
        assert f.check_notional(Decimal("0.001"), Decimal("50000")) is True

    def test_below_min_fails(self):
        f = make_filters(min_notional=5.0)
        assert f.check_notional(Decimal("0.0001"), Decimal("100")) is False
