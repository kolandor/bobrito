"""Unit tests for UI view model builders and service helpers."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bobrito.ui.services import UIService, _format_uptime, _pnl_class


class TestFormatUptime:
    def test_zero(self):
        assert _format_uptime(0) == "0s"

    def test_seconds(self):
        assert _format_uptime(45) == "45s"

    def test_minutes(self):
        assert _format_uptime(90) == "1m 30s"

    def test_hours(self):
        assert _format_uptime(7200) == "2h 0m"

    def test_hours_and_minutes(self):
        assert _format_uptime(3661) == "1h 1m"

    def test_negative_returns_0s(self):
        assert _format_uptime(-5) == "0s"


class TestPnlClass:
    def test_positive(self):
        assert _pnl_class(10.5) == "text-green-400"

    def test_negative(self):
        assert _pnl_class(-5.0) == "text-red-400"

    def test_zero(self):
        assert _pnl_class(0.0) == "text-slate-400"

    def test_none(self):
        assert _pnl_class(None) == "text-slate-400"


def _make_bot(status="running", paused=False, safe_mode=False, snapshot_count=10):
    """Create a minimal TradingBot mock for UIService."""
    bot = MagicMock()
    bot.status.value = status
    bot.get_status_dict.return_value = {
        "status": status,
        "mode": "paper",
        "symbol": "BTCUSDT",
        "uptime_seconds": 3600.0,
        "paused": paused,
        "snapshot_count": snapshot_count,
        "safe_mode": safe_mode,
        "feed_lag_seconds": 1.5,
        "has_open_position": False,
    }
    portfolio = MagicMock()
    portfolio.stats.return_value = {
        "total_trades": 5,
        "wins": 3,
        "losses": 2,
        "win_rate_pct": 60.0,
        "total_pnl_usdt": 12.34,
        "max_drawdown_pct": 1.5,
    }
    portfolio.get_open_position.return_value = None
    bot.get_portfolio.return_value = portfolio

    risk = MagicMock()
    risk.daily_pnl = 5.0
    risk.state_dict.return_value = {
        "safe_mode": safe_mode,
        "daily_trades": 2,
        "daily_pnl": 5.0,
        "consecutive_losses": 0,
        "current_day": "2026-03-15",
    }
    risk.get_params.return_value = {
        "max_consecutive_losses": {"value": 3, "default": 3, "overridden": False},
        "max_daily_loss_pct": {"value": 3.0, "default": 3.0, "overridden": False},
        "cooldown_minutes_after_losses": {"value": 60, "default": 60, "overridden": False},
        "max_trades_per_day": {"value": 10, "default": 10, "overridden": False},
        "min_free_balance_usdt": {"value": 50.0, "default": 50.0, "overridden": False},
    }
    bot.get_risk.return_value = risk
    bot.get_last_snapshot.return_value = None
    return bot


def _make_settings(
    max_daily_loss_pct=3.0, max_consecutive_losses=3, max_trades_per_day=10
):
    s = MagicMock()
    s.max_daily_loss_pct = max_daily_loss_pct
    s.max_consecutive_losses = max_consecutive_losses
    s.max_trades_per_day = max_trades_per_day
    return s


class TestUIServiceBotStatus:
    def test_running_state(self):
        bot = _make_bot(status="running")
        svc = UIService(bot, _make_settings())
        vm = svc.get_bot_status()
        assert vm.status == "running"
        assert vm.uptime_formatted == "1h 0m"
        assert vm.symbol == "BTCUSDT"
        assert vm.mode == "paper"
        assert "green" in vm.status_color

    def test_paused_state(self):
        bot = _make_bot(status="paused", paused=True)
        svc = UIService(bot, _make_settings())
        vm = svc.get_bot_status()
        assert vm.status == "paused"
        assert "yellow" in vm.status_color

    def test_safe_mode_flag(self):
        bot = _make_bot(safe_mode=True)
        svc = UIService(bot, _make_settings())
        vm = svc.get_bot_status()
        assert vm.safe_mode is True

    def test_feed_lag_formatted(self):
        bot = _make_bot()
        svc = UIService(bot, _make_settings())
        vm = svc.get_bot_status()
        assert vm.feed_lag_formatted is not None
        assert "s" in vm.feed_lag_formatted

    def test_zero_snapshots(self):
        bot = _make_bot(snapshot_count=0)
        svc = UIService(bot, _make_settings())
        vm = svc.get_bot_status()
        assert vm.snapshot_count == 0


class TestUIServiceMetrics:
    def test_metrics_aggregation(self):
        bot = _make_bot()
        svc = UIService(bot, _make_settings())
        vm = svc.get_metrics()
        assert vm.total_trades == 5
        assert vm.wins == 3
        assert vm.losses == 2
        assert vm.win_rate == 60.0
        assert vm.total_pnl == 12.34
        assert vm.max_drawdown == 1.5
        assert vm.daily_pnl == 5.0

    def test_positive_pnl_class(self):
        bot = _make_bot()
        svc = UIService(bot, _make_settings())
        vm = svc.get_metrics()
        assert vm.total_pnl_class == "text-green-400"

    def test_negative_pnl_class(self):
        bot = _make_bot()
        bot.get_risk.return_value.daily_pnl = -10.0
        bot.get_portfolio.return_value.stats.return_value["total_pnl_usdt"] = -5.0
        svc = UIService(bot, _make_settings())
        vm = svc.get_metrics()
        assert vm.total_pnl_class == "text-red-400"


class TestUIServiceRiskStatus:
    def test_risk_status(self):
        bot = _make_bot()
        s = _make_settings(max_daily_loss_pct=3.0, max_consecutive_losses=3, max_trades_per_day=10)
        svc = UIService(bot, s)
        vm = svc.get_risk_status()
        assert vm.daily_trades == 2
        assert vm.consecutive_losses == 0
        assert vm.current_day == "2026-03-15"
        assert vm.max_trades_per_day == 10
        assert vm.max_consecutive_losses == 3

    def test_safe_mode_propagated(self):
        bot = _make_bot(safe_mode=True)
        svc = UIService(bot, _make_settings())
        vm = svc.get_risk_status()
        assert vm.safe_mode is True


class TestUIServicePosition:
    def test_no_position(self):
        bot = _make_bot()
        svc = UIService(bot, _make_settings())
        vm = svc.get_position()
        assert vm.has_position is False

    def test_open_position(self):
        bot = _make_bot()
        pos = MagicMock()
        pos.symbol = "BTCUSDT"
        pos.side = "BUY"
        pos.entry_price = 50000.0
        pos.quantity = 0.001
        pos.stop_price = 49000.0
        pos.target_price = 52000.0
        pos.unrealised_pnl = 1.5
        pos.entry_time = datetime(2026, 3, 15, 10, 0, 0)
        bot.get_portfolio.return_value.get_open_position.return_value = pos
        svc = UIService(bot, _make_settings())
        vm = svc.get_position()
        assert vm.has_position is True
        assert vm.entry_price == 50000.0
        assert vm.pnl_class == "text-green-400"


class TestUIServiceBalances:
    @pytest.mark.asyncio
    async def test_broker_not_initialised(self):
        bot = _make_bot()
        bot._broker = None
        svc = UIService(bot, _make_settings())
        vm = await svc.get_balances()
        assert vm.error is not None
        assert vm.free_usdt == 0.0

    @pytest.mark.asyncio
    async def test_broker_returns_balances(self):
        bot = _make_bot()
        broker = AsyncMock()
        broker.get_balances = AsyncMock(return_value={"USDT": 150.0, "BTC": 0.005})
        bot._broker = broker
        snap = MagicMock()
        snap.last_price = 50000.0
        bot.get_last_snapshot.return_value = snap
        svc = UIService(bot, _make_settings())
        vm = await svc.get_balances()
        assert vm.free_usdt == 150.0
        assert vm.free_btc == 0.005
        assert vm.equity_usdt is not None
        assert vm.error is None

    @pytest.mark.asyncio
    async def test_broker_exception_returns_error_vm(self):
        bot = _make_bot()
        broker = AsyncMock()
        broker.get_balances = AsyncMock(side_effect=RuntimeError("network error"))
        bot._broker = broker
        svc = UIService(bot, _make_settings())
        vm = await svc.get_balances()
        assert vm.error is not None
