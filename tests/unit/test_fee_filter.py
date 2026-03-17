"""Unit tests for fee-aware entry filter (check_fee_filter)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from bobrito.risk.manager import RiskManager


def make_settings(**overrides):
    from bobrito.config.settings import Settings

    defaults = {
        "bot_mode": "paper",
        "min_expected_edge_enabled": True,
        "estimated_roundtrip_fee_bps": 20.0,
        "estimated_roundtrip_slippage_bps": 10.0,
        "min_expected_net_edge_bps": 15.0,
        "min_target_distance_bps": 45.0,
    }
    defaults.update(overrides)
    return Settings.model_construct(**defaults)


def make_db_mock():
    db = MagicMock()
    session_ctx = AsyncMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session_ctx)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    session_ctx.add = MagicMock()
    session_ctx.commit = AsyncMock()
    db.session = MagicMock(return_value=session_ctx)
    return db


@pytest.mark.asyncio
class TestCheckFeeFilter:
    async def test_disabled_always_allows(self):
        settings = make_settings(min_expected_edge_enabled=False)
        rm = RiskManager(settings, make_db_mock())
        from bobrito.execution.base import SymbolFilters
        from decimal import Decimal
        rm.configure_filters(
            SymbolFilters(
                symbol="BTCUSDT",
                step_size=Decimal("0.00001"),
                min_qty=Decimal("0.00001"),
                min_notional=Decimal("5.0"),
                tick_size=Decimal("0.01"),
            )
        )
        decision = await rm.check_fee_filter(40000.0, 40100.0)
        assert decision.allowed
        assert "disabled" in decision.reason.lower()

    async def test_passes_when_distance_and_edge_sufficient(self):
        # entry=40000, target=40500 -> distance = 500/40000 * 10000 = 125 bps
        # cost = 20 + 10 = 30 bps
        # net edge = 125 - 30 = 95 bps > 15, distance 125 > 45
        settings = make_settings()
        rm = RiskManager(settings, make_db_mock())
        from bobrito.execution.base import SymbolFilters
        from decimal import Decimal
        rm.configure_filters(
            SymbolFilters(
                symbol="BTCUSDT",
                step_size=Decimal("0.00001"),
                min_qty=Decimal("0.00001"),
                min_notional=Decimal("5.0"),
                tick_size=Decimal("0.01"),
            )
        )
        decision = await rm.check_fee_filter(40000.0, 40500.0)
        assert decision.allowed

    async def test_rejects_min_target_distance(self):
        # entry=40000, target=40050 -> distance = 50/40000 * 10000 = 12.5 bps < 45
        settings = make_settings(min_target_distance_bps=45.0)
        rm = RiskManager(settings, make_db_mock())
        from bobrito.execution.base import SymbolFilters
        from decimal import Decimal
        rm.configure_filters(
            SymbolFilters(
                symbol="BTCUSDT",
                step_size=Decimal("0.00001"),
                min_qty=Decimal("0.00001"),
                min_notional=Decimal("5.0"),
                tick_size=Decimal("0.01"),
            )
        )
        decision = await rm.check_fee_filter(40000.0, 40050.0)
        assert not decision.allowed
        assert "distance" in decision.reason.lower() or "target" in decision.reason.lower()

    async def test_rejects_min_expected_edge(self):
        # entry=40000, target=40080 -> distance = 80/40000 * 10000 = 20 bps
        # cost = 30 bps -> net = -10 bps < 15
        settings = make_settings(min_target_distance_bps=10.0, min_expected_net_edge_bps=15.0)
        rm = RiskManager(settings, make_db_mock())
        from bobrito.execution.base import SymbolFilters
        from decimal import Decimal
        rm.configure_filters(
            SymbolFilters(
                symbol="BTCUSDT",
                step_size=Decimal("0.00001"),
                min_qty=Decimal("0.00001"),
                min_notional=Decimal("5.0"),
                tick_size=Decimal("0.01"),
            )
        )
        decision = await rm.check_fee_filter(40000.0, 40080.0)
        assert not decision.allowed
        assert "edge" in decision.reason.lower()
