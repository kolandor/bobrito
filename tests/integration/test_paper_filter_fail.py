"""Integration tests: paper mode when exchange filters cannot be loaded."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bobrito.config.settings import BotMode, Settings
from bobrito.engine.bot import TradingBot
from bobrito.execution.paper import PaperBroker
from bobrito.strategy.base import Signal, SignalType


@pytest.mark.asyncio
async def test_paper_mode_filter_fail_activates_safe_mode_and_blocks_entries():
    """When filters cannot be loaded in paper mode: safe mode activates, no new entries.

    PaperBroker returns None from get_symbol_filters before set_filters is called.
    The engine treats that as filter unavailability and activates safe mode in paper.
    """
    settings = Settings.model_construct(
        bot_mode=BotMode.PAPER,
        symbol="BTCUSDT",
        allow_filter_fallback=False,
        initial_capital_usdt=500.0,
        paper_initial_usdt=500.0,
        risk_per_trade_pct=1.0,
        max_daily_loss_pct=5.0,
        max_consecutive_losses=5,
        cooldown_minutes_after_losses=0,
        max_trades_per_day=10,
        min_free_balance_usdt=10.0,
    )
    db = MagicMock()
    sess_ctx = AsyncMock()
    sess_ctx.__aenter__ = AsyncMock(return_value=sess_ctx)
    sess_ctx.__aexit__ = AsyncMock(return_value=False)
    sess_ctx.add = MagicMock()
    sess_ctx.commit = AsyncMock()
    sess_ctx.refresh = AsyncMock()
    sess_ctx.get = AsyncMock(return_value=None)
    # load_daily_stats: first execute (agg), second execute (recent)
    row1 = MagicMock()
    row1.trades = 0
    row1.pnl = 0.0
    result1 = MagicMock()
    result1.one = lambda: row1
    result2 = MagicMock()
    result2.all = lambda: []
    # load_historical_stats: one execute
    row3 = MagicMock()
    row3.total = 0
    row3.total_pnl = 0.0
    row3.wins = 0
    row3.losses = 0
    result3 = MagicMock()
    result3.one = lambda: row3
    # Order: load_historical_stats (1), load_daily_stats agg (2), load_daily_stats recent (3)
    sess_ctx.execute = AsyncMock(side_effect=[result3, result1, result2])
    db.session = MagicMock(return_value=sess_ctx)

    # PaperBroker returns None from get_symbol_filters when _filters not set
    broker = PaperBroker(initial_usdt=500.0, fee_rate=0.001, slippage_bps=0.0)
    assert await broker.get_symbol_filters("BTCUSDT") is None

    bot = TradingBot(settings, db)
    with patch.object(bot, "_create_broker", return_value=broker):
        with patch.object(bot, "_prefill_candle_buffers", new=AsyncMock()):
            with patch.object(bot, "_restore_paper_state", new=AsyncMock()):
                with patch("bobrito.engine.bot.MarketDataFeed") as mock_feed_cls:
                    mock_feed = MagicMock()
                    mock_feed.start = AsyncMock()
                    mock_feed.stop = AsyncMock()
                    mock_feed_cls.return_value = mock_feed
                    await bot.start()

    assert bot.get_risk().safe_mode is True
    signal = Signal(
        signal_type=SignalType.BUY,
        symbol="BTCUSDT",
        price=40000.0,
        timestamp=datetime.utcnow(),
        stop_price=39000.0,
        target_price=43000.0,
    )
    decision = await bot.get_risk().validate_entry(
        signal, free_usdt=300.0, has_open_position=False
    )
    assert not decision.allowed
    assert "safe mode" in decision.reason.lower()

    await bot.stop()
