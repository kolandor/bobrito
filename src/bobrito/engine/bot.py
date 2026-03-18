"""Main Bot Engine — orchestrates all layers.

Lifecycle:
  1. start()  → initialise DB, broker, feed; begin processing loop
  2. stop()   → graceful shutdown
  3. pause()  → suspend new entries (exits still processed)
  4. resume() → re-enable entries
  5. emergency_stop() → immediate halt, close open position at market

Data flow:
  MarketDataFeed → on_snapshot() → strategy.evaluate() → risk.validate()
  → broker.place_order() → portfolio.open_position()

Exit monitoring runs on every snapshot when a position is open.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from decimal import Decimal
from enum import StrEnum

from bobrito.config.settings import Settings
from bobrito.execution.base import (
    BrokerBase,
    OrderRequest,
    OrderSide,
    OrderType,
    SymbolFilters,
)
from bobrito.execution.binance import BinanceBroker
from bobrito.execution.paper import PaperBroker
from bobrito.market_data.buffer import CandleBuffer
from bobrito.market_data.feed import MarketDataFeed
from bobrito.market_data.history import BINANCE_PUBLIC_REST, HistoricalLoader
from bobrito.market_data.models import MarketSnapshot
from bobrito.monitoring.logger import get_logger
from bobrito.monitoring.metrics import MetricsCollector
from bobrito.persistence.database import DatabaseManager
from bobrito.persistence.models import ExitReason, SystemEvent
from bobrito.persistence.models import Signal as DBSignal
from bobrito.portfolio.manager import PortfolioManager
from bobrito.risk.manager import RiskManager
from bobrito.strategy.base import SignalType
from bobrito.strategy.trend_pullback import TrendPullbackStrategy

log = get_logger("engine.bot")


class BotStatus(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class TradingBot:
    def __init__(self, settings: Settings, db: DatabaseManager) -> None:
        self._s = settings
        self._db = db
        self._status = BotStatus.IDLE
        self._start_time: float | None = None
        self._paused = False

        # Candle buffers
        self._buf1 = CandleBuffer("1m", maxlen=settings.candle_buffer_size)
        self._buf5 = CandleBuffer("5m", maxlen=settings.candle_buffer_size)

        # Sub-components (initialised in start())
        self._broker: BrokerBase | None = None
        self._feed: MarketDataFeed | None = None
        self._strategy = TrendPullbackStrategy(
            ema_fast=settings.ema_fast,
            ema_slow=settings.ema_slow,
            atr_period=settings.atr_period,
            volume_multiplier=settings.volume_multiplier,
            atr_stop_mult=settings.stop_atr_multiplier,
            atr_target_mult=settings.target_atr_multiplier,
            ema_min_separation_pct=settings.ema_min_separation_pct,
            pullback_lookback_bars=settings.pullback_lookback_bars,
            pullback_near_slow_ema_pct=settings.pullback_near_slow_ema_pct,
            volume_sma_period=settings.volume_sma_period,
            swing_low_lookback=settings.swing_low_lookback,
            min_1m_warmup=settings.min_1m_warmup_candles,
            min_5m_warmup=settings.min_5m_warmup_candles,
            momentum_failure_confirm_bars=settings.momentum_failure_confirm_bars,
            momentum_failure_min_hold_bars=settings.momentum_failure_min_hold_bars,
            momentum_failure_exit_ema=settings.momentum_failure_exit_ema,
        )
        self._risk = RiskManager(settings, db)
        self._portfolio = PortfolioManager(settings, db)

        self._last_snapshot: MarketSnapshot | None = None
        self._snapshot_count: int = 0
        self._midnight_reset_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._status not in (BotStatus.IDLE, BotStatus.STOPPED):
            raise RuntimeError(f"Cannot start bot in state {self._status}")

        self._status = BotStatus.STARTING
        log.info(f"Starting bot in {self._s.bot_mode.value.upper()} mode…")
        await self._log_system_event("BOT_START", f"Mode: {self._s.bot_mode.value}")

        # Safety: live trading gate
        if self._s.is_live() and not self._s.live_trading_enabled:
            raise RuntimeError(
                "Live trading requested but LIVE_TRADING_ENABLED is not set. "
                "Set it explicitly in .env after careful review."
            )

        self._broker = self._create_broker()

        # Fetch and apply exchange filters
        filters = await self._broker.get_symbol_filters(self._s.symbol)
        if filters is None:
            if self._s.allow_filter_fallback:
                log.warning(
                    "Exchange filters unavailable — using fallback. NOT recommended for live trading."
                )
                filters = SymbolFilters(
                    symbol=self._s.symbol,
                    step_size=Decimal(str(self._s.fallback_step_size)),
                    min_qty=Decimal(str(self._s.fallback_min_qty)),
                    min_notional=Decimal(str(self._s.fallback_min_notional)),
                    tick_size=Decimal(str(self._s.fallback_tick_size)),
                )
            elif self._s.is_live() or self._s.is_testnet():
                raise RuntimeError(
                    "Cannot start: exchange filters unavailable. "
                    "Set ALLOW_FILTER_FALLBACK=true only if you understand the risks."
                )
            else:
                self._risk.activate_safe_mode("Exchange filters unavailable")
                filters = SymbolFilters(
                    symbol=self._s.symbol,
                    step_size=Decimal(str(self._s.fallback_step_size)),
                    min_qty=Decimal(str(self._s.fallback_min_qty)),
                    min_notional=Decimal(str(self._s.fallback_min_notional)),
                    tick_size=Decimal(str(self._s.fallback_tick_size)),
                )
        self._risk.configure_filters(filters)
        if isinstance(self._broker, PaperBroker):
            self._broker.set_filters(filters)

        # ── Historical pre-fill (eliminates EMA warm-up blind period) ────────
        await self._prefill_candle_buffers()

        ws_url = self._s.active_ws_url()
        self._feed = MarketDataFeed(
            symbol=self._s.symbol,
            ws_base_url=ws_url,
            buffer_1m=self._buf1,
            buffer_5m=self._buf5,
            on_snapshot=self._on_snapshot,
        )

        # Bootstrap cumulative and daily stats from DB so the dashboard
        # always reflects lifetime results, not just the current session.
        await self._portfolio.load_historical_stats()
        await self._risk.load_daily_stats()

        # For paper trading: reconstruct the actual account balance and any
        # in-progress open position from the database, so restarts are seamless.
        if isinstance(self._broker, PaperBroker):
            await self._restore_paper_state()

        await self._feed.start()
        self._start_time = time.time()
        self._status = BotStatus.RUNNING
        MetricsCollector.ws_connected.set(1)

        self._midnight_reset_task = asyncio.create_task(
            self._risk.run_midnight_reset_loop(),
            name="midnight_reset",
        )

        log.info("Bot running — waiting for market data…")

    async def stop(self) -> None:
        self._status = BotStatus.STOPPING
        if self._midnight_reset_task and not self._midnight_reset_task.done():
            self._midnight_reset_task.cancel()
        if self._feed:
            await self._feed.stop()
        if isinstance(self._broker, BinanceBroker):
            await self._broker.close()
        await self._log_system_event("BOT_STOP", "Graceful shutdown")
        self._status = BotStatus.STOPPED
        log.info("Bot stopped")

    def pause(self) -> None:
        self._paused = True
        self._status = BotStatus.PAUSED
        log.info("Bot PAUSED — exits still monitored, no new entries")

    def resume(self) -> None:
        self._paused = False
        self._status = BotStatus.RUNNING
        log.info("Bot RESUMED")

    async def emergency_stop(self) -> None:
        """Close open position at market, then stop."""
        log.warning("EMERGENCY STOP triggered")
        self._risk.activate_safe_mode("Emergency stop")

        if self._portfolio.has_open_position() and self._last_snapshot:
            await self._execute_exit(self._last_snapshot, ExitReason.EMERGENCY)

        await self.stop()
        await self._log_system_event("EMERGENCY_STOP", "Emergency stop executed")

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def status(self) -> BotStatus:
        return self._status

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._start_time if self._start_time else 0.0

    def get_status_dict(self) -> dict:
        MetricsCollector.uptime_seconds.set(self.uptime_seconds)
        return {
            "status": self._status.value,
            "mode": self._s.bot_mode.value,
            "symbol": self._s.symbol,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "paused": self._paused,
            "snapshot_count": self._snapshot_count,
            "safe_mode": self._risk.safe_mode,
            "risk": self._risk.state_dict(),
            "portfolio": self._portfolio.stats(),
            "has_open_position": self._portfolio.has_open_position(),
            "feed_lag_seconds": round(self._feed.feed_lag_seconds, 2) if self._feed else None,
        }

    def get_portfolio(self) -> PortfolioManager:
        return self._portfolio

    def get_risk(self) -> RiskManager:
        return self._risk

    def get_last_snapshot(self) -> MarketSnapshot | None:
        return self._last_snapshot

    # ── Paper-mode state restoration ───────────────────────────────────────

    async def _restore_paper_state(self) -> None:
        """Reconstruct PaperBroker balance and in-progress position from DB.

        Walk the closed positions to compute total realised PnL, then check
        for an open position.  The resulting USDT/BTC balances are pushed into
        the PaperBroker so risk management and position sizing work correctly
        from the first snapshot after a restart.
        """
        from sqlalchemy import func, select

        from bobrito.persistence.models import Position as DBPos
        from bobrito.persistence.models import PositionStatus

        initial = self._s.initial_capital_usdt

        async with self._db.session() as sess:
            pnl_row = await sess.execute(
                select(func.coalesce(func.sum(DBPos.net_pnl), 0.0)).where(
                    DBPos.status == PositionStatus.CLOSED
                )
            )
            total_pnl = float(pnl_row.scalar() or 0.0)

        # Restore any open position into PortfolioManager
        open_pos = await self._portfolio.restore_open_position()

        free_usdt = initial + total_pnl
        free_btc = 0.0

        if open_pos:
            # The USDT cost of the open position was already deducted when
            # the entry order was placed, so subtract it again.
            cost = open_pos.entry_price * open_pos.quantity + open_pos.fees
            free_usdt -= cost
            free_btc = open_pos.quantity

        free_usdt = max(free_usdt, 0.0)

        log.info(
            f"Paper state restored from DB: "
            f"initial={initial:.2f} realised_pnl={total_pnl:+.4f} "
            f"→ free_usdt={free_usdt:.2f} free_btc={free_btc:.6f}"
            + (f" (open position id={open_pos.db_id})" if open_pos else "")
        )

        assert isinstance(self._broker, PaperBroker)
        self._broker.restore_balances(free_usdt, free_btc)

    # ── Core processing loop ───────────────────────────────────────────────

    async def _on_snapshot(self, snapshot: MarketSnapshot) -> None:
        self._last_snapshot = snapshot
        self._snapshot_count += 1

        # Update paper broker price
        if isinstance(self._broker, PaperBroker):
            self._broker.update_price(snapshot.last_price)

        # Update unrealised PnL for open position
        pos = self._portfolio.get_open_position()
        if pos:
            pos.update_unrealised(snapshot.last_price)

        if self._status not in (BotStatus.RUNNING, BotStatus.PAUSED):
            return

        try:
            await self._process_snapshot(snapshot)
        except (TypeError, AttributeError, ValueError) as exc:
            # Programming errors — log but do NOT trigger safe mode so the
            # bug can be fixed and the bot restarted without a stuck safe-mode latch.
            log.exception(f"Bug in snapshot processing (safe mode NOT activated): {exc}")
        except Exception as exc:
            # Unexpected runtime errors (network, DB, exchange) → safe mode.
            log.exception(f"Critical error processing snapshot: {exc}")
            self._risk.activate_safe_mode(f"Processing error: {exc}")

    async def _process_snapshot(self, snapshot: MarketSnapshot) -> None:
        has_position = self._portfolio.has_open_position()

        # ── Check stop/target when position is open ────────────────────────
        if has_position:
            pos = self._portfolio.get_open_position()
            assert pos is not None
            price = snapshot.last_price
            if pos.is_stop_hit(price):
                log.info(f"Stop hit @ {price:.2f} (stop={pos.stop_price:.2f})")
                await self._execute_exit(snapshot, ExitReason.STOP_LOSS)
                return
            if pos.is_target_hit(price):
                log.info(f"Target hit @ {price:.2f} (target={pos.target_price:.2f})")
                await self._execute_exit(snapshot, ExitReason.TAKE_PROFIT)
                return

        # ── Get strategy signal ───────────────────────────────────────────
        signal = self._strategy.evaluate(snapshot, has_position)

        # ── Persist signal ────────────────────────────────────────────────
        db_signal = await self._persist_signal(signal)

        # ── Exit on momentum failure ──────────────────────────────────────
        if has_position and signal.signal_type == SignalType.EXIT:
            await self._execute_exit(snapshot, ExitReason.MOMENTUM_FAILURE)
            return

        # ── Entry (not paused, not in position) ───────────────────────────
        if self._paused or has_position or signal.signal_type != SignalType.BUY:
            return

        # ── Fee-aware filter (before risk validation) ──────────────────────
        if signal.target_price:
            fee_decision = await self._risk.check_fee_filter(signal.price, signal.target_price)
            if not fee_decision.allowed:
                log.debug(f"Entry blocked by fee filter: {fee_decision.reason}")
                async with self._db.session() as sess:
                    db_row = await sess.get(DBSignal, db_signal.id)
                    if db_row:
                        db_row.rejected_reason = fee_decision.reason
                        await sess.commit()
                return

        # ── Risk validation ───────────────────────────────────────────────
        assert self._broker is not None
        balances = await self._broker.get_balances()
        free_usdt = balances.get("USDT", 0.0)

        decision = await self._risk.validate_entry(signal, free_usdt, has_open_position=False)

        if not decision.allowed:
            log.debug(f"Entry blocked by risk: {decision.reason}")
            # Update DB signal with rejection reason
            async with self._db.session() as sess:
                db_row = await sess.get(DBSignal, db_signal.id)
                if db_row:
                    db_row.rejected_reason = decision.reason
                    await sess.commit()
            return

        # ── Execute entry order ───────────────────────────────────────────
        await self._execute_entry(
            snapshot, signal, decision.quantity, decision.risk_amount, db_signal.id
        )

    async def _execute_entry(
        self, snapshot: MarketSnapshot, signal, quantity: float, risk_amount: float, signal_id: int
    ) -> None:
        assert self._broker is not None
        request = OrderRequest(
            symbol=self._s.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=quantity,
            client_order_id=str(uuid.uuid4()).replace("-", "")[:36],
        )

        order = await self._broker.place_order(request)
        if not order.is_filled:
            log.warning(f"Entry order not filled: {order.status}")
            return

        await self._persist_order(order, signal_id)
        await self._portfolio.open_position(
            order=order,
            stop_price=signal.stop_price,
            target_price=signal.target_price,
            risk_amount=risk_amount,
            signal_id=signal_id,
        )
        self._strategy.reset_position_tracking()

        # Update signal as acted-on
        async with self._db.session() as sess:
            db_sig = await sess.get(DBSignal, signal_id)
            if db_sig:
                db_sig.acted_on = True
                await sess.commit()

    async def _execute_exit(self, snapshot: MarketSnapshot, reason: ExitReason) -> None:
        assert self._broker is not None
        pos = self._portfolio.get_open_position()
        if pos is None:
            return

        request = OrderRequest(
            symbol=self._s.symbol,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=pos.quantity,
            client_order_id=str(uuid.uuid4()).replace("-", "")[:36],
        )

        order = await self._broker.place_order(request)
        await self._persist_order(order)

        balances = await self._broker.get_balances()
        free_usdt = balances.get("USDT", 0.0)
        free_btc = balances.get("BTC", 0.0)

        net_pnl = await self._portfolio.close_position(order, reason, free_usdt)
        await self._risk.record_trade_result(net_pnl)

        await self._portfolio.snapshot_equity(
            free_usdt=free_usdt,
            free_btc=free_btc,
            btc_price=snapshot.last_price,
        )
        MetricsCollector.daily_profit_usdt.set(self._risk.daily_pnl)

    # ── Persistence helpers ───────────────────────────────────────────────

    async def _persist_signal(self, signal) -> DBSignal:
        db_signal = DBSignal(
            symbol=signal.symbol,
            mode=self._s.bot_mode.value,
            signal_type=signal.signal_type.value,
            price=signal.price,
            stop_price=signal.stop_price,
            target_price=signal.target_price,
            atr=signal.atr,
            ema_fast=signal.ema_fast,
            ema_slow=signal.ema_slow,
            regime=signal.regime.value if signal.regime else None,
            explanation=signal.explanation,
            acted_on=False,
        )
        async with self._db.session() as sess:
            sess.add(db_signal)
            await sess.commit()
            await sess.refresh(db_signal)
        return db_signal

    async def _persist_order(self, order, signal_id: int | None = None) -> None:
        from bobrito.persistence.models import (
            Order as DBOrder,
        )

        db_order = DBOrder(
            symbol=order.symbol,
            mode=self._s.bot_mode.value,
            client_order_id=order.client_order_id,
            exchange_order_id=order.exchange_order_id,
            side=order.side.value,
            order_type=order.order_type.value,
            status=order.status.value,
            quantity=order.requested_qty,
            filled_quantity=order.filled_qty,
            average_fill_price=order.average_price,
            commission=order.commission,
            commission_asset=order.commission_asset,
        )
        async with self._db.session() as sess:
            sess.add(db_order)
            await sess.commit()

    # ── Factory ───────────────────────────────────────────────────────────

    def _create_broker(self) -> BrokerBase:
        s = self._s
        if s.is_paper():
            log.info("Using PaperBroker")
            return PaperBroker(
                initial_usdt=s.paper_initial_usdt,
                fee_rate=s.paper_fee_rate,
                slippage_bps=s.paper_slippage_bps,
            )
        elif s.is_testnet():
            log.info("Using BinanceBroker (Testnet)")
            return BinanceBroker(
                api_key=s.binance_testnet_api_key,
                api_secret=s.binance_testnet_api_secret,
                base_url=s.binance_testnet_rest_url,
                mode="testnet",
            )
        else:
            log.info("Using BinanceBroker (LIVE)")
            return BinanceBroker(
                api_key=s.binance_live_api_key,
                api_secret=s.binance_live_api_secret,
                base_url=s.binance_live_rest_url,
                mode="live",
            )

    async def _prefill_candle_buffers(self) -> None:
        """Download historical candles from Binance REST and pre-fill buffers.

        Uses the public klines endpoint — no API key required.
        Falls back gracefully if the request fails so the bot still starts
        (it will just need to warm up from the live stream instead).
        """
        loader = HistoricalLoader(
            symbol=self._s.symbol,
            rest_base_url=BINANCE_PUBLIC_REST,
        )
        try:
            await loader.prefill(
                buf1m=self._buf1,
                buf5m=self._buf5,
                limit=self._s.candle_buffer_size,
            )
            log.info(
                f"Candle buffers ready: " f"1m={len(self._buf1)} bars, 5m={len(self._buf5)} bars"
            )
        except Exception as exc:
            log.warning(
                f"Historical pre-fill failed: {exc}. " "Bot will warm up from live stream instead."
            )

    async def _log_system_event(self, event_type: str, description: str = "") -> None:
        ev = SystemEvent(
            event_type=event_type,
            description=description,
            mode=self._s.bot_mode.value,
        )
        async with self._db.session() as sess:
            sess.add(ev)
            await sess.commit()
