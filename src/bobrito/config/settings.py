"""Centralised configuration via pydantic-settings.

All environment variables are documented in .env.example.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotMode(StrEnum):
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Runtime ──────────────────────────────────────────────────────────────
    bot_mode: BotMode = BotMode.PAPER

    # ── Trading pair (fixed for v1) ───────────────────────────────────────────
    symbol: str = "BTCUSDT"
    base_asset: str = "BTC"
    quote_asset: str = "USDT"

    # ── Binance Testnet ───────────────────────────────────────────────────────
    binance_testnet_api_key: str = ""
    binance_testnet_api_secret: str = ""
    binance_testnet_rest_url: str = "https://testnet.binance.vision"
    binance_testnet_ws_url: str = "wss://testnet.binance.vision"

    # ── Binance Live ──────────────────────────────────────────────────────────
    binance_live_api_key: str = ""
    binance_live_api_secret: str = ""
    binance_live_rest_url: str = "https://api.binance.com"
    binance_live_ws_url: str = "wss://stream.binance.com:9443"

    # ── Safety gate ───────────────────────────────────────────────────────────
    live_trading_enabled: bool = False

    # ── Capital ───────────────────────────────────────────────────────────────
    initial_capital_usdt: float = 200.0
    paper_initial_usdt: float = 200.0

    # ── Exchange filters (fallback when exchangeInfo unavailable) ──────────────
    allow_filter_fallback: bool = False
    fallback_step_size: float = 0.00001
    fallback_min_qty: float = 0.00001
    fallback_min_notional: float = 5.0
    fallback_tick_size: float = 0.01

    # ── Strategy ─────────────────────────────────────────────────────────────
    ema_fast: int = 9
    ema_slow: int = 21
    atr_period: int = 14
    volume_multiplier: float = 1.5
    # Trend TF / entry TF (informational; used by feed layer)
    trend_interval: str = "5m"
    entry_interval: str = "1m"
    candle_buffer_size: int = 500
    # Newly externalised strategy params
    ema_min_separation_pct: float = 0.05
    pullback_lookback_bars: int = 5
    pullback_near_slow_ema_pct: float = 0.2
    volume_sma_period: int = 20
    stop_atr_multiplier: float = 1.5
    target_atr_multiplier: float = 3.0
    min_1m_warmup_candles: int = 30
    min_5m_warmup_candles: int = 25
    swing_low_lookback: int = 5

    # ── Fee-aware entry filter ────────────────────────────────────────────────
    min_expected_edge_enabled: bool = True
    estimated_roundtrip_fee_bps: float = 20.0
    estimated_roundtrip_slippage_bps: float = 10.0
    min_expected_net_edge_bps: float = 15.0
    min_target_distance_bps: float = 45.0

    # ── Momentum Failure exit ─────────────────────────────────────────────────
    momentum_failure_confirm_bars: int = 2
    momentum_failure_min_hold_bars: int = 2
    momentum_failure_exit_ema: str = "fast"

    # ── Risk ─────────────────────────────────────────────────────────────────
    risk_per_trade_pct: float = Field(0.75, ge=0.1, le=2.0)
    max_daily_loss_pct: float = Field(3.0, ge=0.5, le=10.0)
    max_consecutive_losses: int = Field(3, ge=1)
    cooldown_minutes_after_losses: int = Field(60, ge=0)
    max_trades_per_day: int = Field(10, ge=1)
    min_free_balance_usdt: float = Field(20.0, ge=0.0)

    # ── Paper trading slippage ────────────────────────────────────────────────
    paper_slippage_bps: float = 5.0  # basis points
    paper_fee_rate: float = 0.001  # 0.1 % taker fee

    # ── Database ─────────────────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./data/bobrito.db"

    # ── API ───────────────────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8080
    api_secret_key: str = "change_me_to_a_random_secret_at_least_32_chars"

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_file: str = "./logs/bobrito.log"

    # ── Prometheus ────────────────────────────────────────────────────────────
    metrics_port: int = 9090

    # ── Web UI ────────────────────────────────────────────────────────────────
    web_ui_enabled: bool = False
    web_ui_route_prefix: str = "/ui"
    web_ui_readonly: bool = False
    web_ui_page_refresh_seconds: int = 5

    # UI session-based authentication
    web_ui_username: str = "admin"
    web_ui_password: str = "change_me_strong_password"
    web_ui_session_secret: str = "change_me_very_long_random_secret_32chars"

    # UI feature flags
    web_ui_show_debug_blocks: bool = False
    web_ui_show_raw_metrics: bool = False
    web_ui_allow_emergency_stop: bool = True
    web_ui_allow_start_stop: bool = True
    web_ui_confirm_live_actions: bool = True

    # ── Validators ───────────────────────────────────────────────────────────
    @field_validator("bot_mode", mode="before")
    @classmethod
    def lower_mode(cls, v: str) -> str:
        return str(v).lower()

    def is_paper(self) -> bool:
        return self.bot_mode == BotMode.PAPER

    def is_testnet(self) -> bool:
        return self.bot_mode == BotMode.TESTNET

    def is_live(self) -> bool:
        return self.bot_mode == BotMode.LIVE

    def active_api_key(self) -> str:
        if self.is_testnet():
            return self.binance_testnet_api_key
        if self.is_live():
            return self.binance_live_api_key
        return ""

    def active_api_secret(self) -> str:
        if self.is_testnet():
            return self.binance_testnet_api_secret
        if self.is_live():
            return self.binance_live_api_secret
        return ""

    def active_rest_url(self) -> str:
        if self.is_testnet():
            return self.binance_testnet_rest_url
        if self.is_live():
            return self.binance_live_rest_url
        # Paper mode uses live public data
        return self.binance_live_rest_url

    def active_ws_url(self) -> str:
        if self.is_testnet():
            return self.binance_testnet_ws_url
        # Paper and live both use real market streams
        return self.binance_live_ws_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
