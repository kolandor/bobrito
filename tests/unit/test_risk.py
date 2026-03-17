"""Unit tests for risk manager: position sizing, rule enforcement."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from decimal import Decimal

from bobrito.execution.base import SymbolFilters
from bobrito.risk.manager import RiskManager, _round_step
from bobrito.strategy.base import Signal, SignalType

# ── Helpers ───────────────────────────────────────────────────────────────────


def make_symbol_filters(step_size: float = 0.00001, min_qty: float = 0.00001, min_notional: float = 5.0) -> SymbolFilters:
    return SymbolFilters(
        symbol="BTCUSDT",
        step_size=Decimal(str(step_size)),
        min_qty=Decimal(str(min_qty)),
        min_notional=Decimal(str(min_notional)),
        tick_size=Decimal("0.01"),
    )

def make_settings(**overrides):
    from bobrito.config.settings import Settings

    defaults = {
        "bot_mode": "paper",
        "initial_capital_usdt": 1000.0,
        "risk_per_trade_pct": 1.0,
        "max_daily_loss_pct": 3.0,
        "max_consecutive_losses": 3,
        "cooldown_minutes_after_losses": 0,
        "max_trades_per_day": 10,
        "min_free_balance_usdt": 50.0,
        "paper_initial_usdt": 1000.0,
    }
    defaults.update(overrides)
    return Settings.model_construct(**defaults)


def make_signal(price: float = 40000.0, stop_price: float = 39000.0) -> Signal:
    return Signal(
        signal_type=SignalType.BUY,
        symbol="BTCUSDT",
        price=price,
        timestamp=datetime.utcnow(),
        stop_price=stop_price,
        target_price=price + 3000,
    )


def make_db_mock():
    db = MagicMock()
    session_ctx = AsyncMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session_ctx)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    session_ctx.add = MagicMock()
    session_ctx.commit = AsyncMock()
    db.session = MagicMock(return_value=session_ctx)
    return db


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestRoundStep:
    def test_basic_rounding(self):
        assert _round_step(0.123456, 0.00001) == pytest.approx(0.12345, rel=1e-5)

    def test_rounds_down(self):
        assert _round_step(1.9999, 0.001) == pytest.approx(1.999, rel=1e-5)

    def test_zero_step(self):
        assert _round_step(1.5, 0.0) == 1.5


class TestPositionSizing:
    def test_basic_sizing(self):
        settings = make_settings(
            initial_capital_usdt=1000.0,
            risk_per_trade_pct=1.0,
            min_free_balance_usdt=0.0,
        )
        rm = RiskManager(settings, make_db_mock())
        rm.configure_filters(make_symbol_filters())

        price = 40000.0
        stop = 39000.0
        stop_dist = price - stop   # = 1000
        free_usdt = 1000.0

        # Sizing is based on tradeable balance (free_usdt - min_free_balance)
        # tradeable = 1000 - 0 = 1000 USDT
        # risk_amount = 1000 * 1% = 10 USDT
        # qty = 10 / 1000 = 0.01 BTC
        qty, risk_amount = rm._calculate_position_size(price, stop_dist, free_usdt=free_usdt)
        assert abs(risk_amount - 10.0) < 0.01
        assert abs(qty - 0.01) < 0.0001

    def test_qty_capped_by_available_balance(self):
        settings = make_settings(
            initial_capital_usdt=10000.0,
            risk_per_trade_pct=1.0,
            min_free_balance_usdt=0.0,
        )
        rm = RiskManager(settings, make_db_mock())
        rm.configure_filters(make_symbol_filters())

        # Only 50 USDT free → max qty at 40000 = 0.00125 BTC
        qty, _ = rm._calculate_position_size(40000.0, 1000.0, free_usdt=50.0)
        assert qty * 40000.0 <= 50.0 + 0.01


@pytest.mark.asyncio
class TestRiskRules:
    async def test_allows_trade_when_clean(self):
        settings = make_settings()
        rm = RiskManager(settings, make_db_mock())
        rm.configure_filters(make_symbol_filters())
        decision = await rm.validate_entry(make_signal(), free_usdt=500.0, has_open_position=False)
        assert decision.allowed

    async def test_blocks_open_position(self):
        settings = make_settings()
        rm = RiskManager(settings, make_db_mock())
        decision = await rm.validate_entry(make_signal(), free_usdt=500.0, has_open_position=True)
        assert not decision.allowed
        assert "open position" in decision.reason.lower()

    async def test_blocks_daily_loss(self):
        settings = make_settings(initial_capital_usdt=1000.0, max_daily_loss_pct=3.0)
        rm = RiskManager(settings, make_db_mock())
        rm.configure_filters(make_symbol_filters())
        # Simulate -35 USDT daily loss (> 3% of 1000)
        rm._daily_realised_pnl = -35.0
        decision = await rm.validate_entry(make_signal(), free_usdt=500.0, has_open_position=False)
        assert not decision.allowed
        assert "daily loss" in decision.reason.lower()

    async def test_blocks_consecutive_losses(self):
        settings = make_settings(max_consecutive_losses=3)
        rm = RiskManager(settings, make_db_mock())
        rm.configure_filters(make_symbol_filters())
        rm._consecutive_losses = 3
        decision = await rm.validate_entry(make_signal(), free_usdt=500.0, has_open_position=False)
        assert not decision.allowed

    async def test_blocks_max_trades(self):
        settings = make_settings(max_trades_per_day=5)
        rm = RiskManager(settings, make_db_mock())
        rm.configure_filters(make_symbol_filters())
        rm._daily_trades = 5
        decision = await rm.validate_entry(make_signal(), free_usdt=500.0, has_open_position=False)
        assert not decision.allowed

    async def test_blocks_min_balance(self):
        settings = make_settings(min_free_balance_usdt=100.0)
        rm = RiskManager(settings, make_db_mock())
        rm.configure_filters(make_symbol_filters())
        decision = await rm.validate_entry(make_signal(), free_usdt=50.0, has_open_position=False)
        assert not decision.allowed

    async def test_safe_mode_blocks_all(self):
        settings = make_settings()
        rm = RiskManager(settings, make_db_mock())
        rm.activate_safe_mode("test")
        decision = await rm.validate_entry(make_signal(), free_usdt=500.0, has_open_position=False)
        assert not decision.allowed
        assert "safe mode" in decision.reason.lower()

    async def test_record_trade_result_increments_losses(self):
        settings = make_settings()
        rm = RiskManager(settings, make_db_mock())
        await rm.record_trade_result(-10.0)
        assert rm.consecutive_losses == 1
        assert rm.daily_pnl == pytest.approx(-10.0)

    async def test_win_resets_consecutive_losses(self):
        settings = make_settings()
        rm = RiskManager(settings, make_db_mock())
        rm._consecutive_losses = 2
        await rm.record_trade_result(5.0)
        assert rm.consecutive_losses == 0
