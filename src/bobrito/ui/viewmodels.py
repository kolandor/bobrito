"""View model dataclasses used as template context for the Web UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RiskBlockVM:
    """A single active risk protection that is currently blocking new entries."""

    type: str           # "safe_mode" | "daily_loss" | "consecutive_losses" | "cooldown" | "max_trades" | "min_balance"
    name: str           # short human-readable label
    reason: str         # why the block is active
    reset_tip: str      # how / when it will clear
    severity: str       # "critical" | "warning"
    remaining_seconds: int = 0   # cooldown only


@dataclass
class BotStatusVM:
    status: str
    mode: str
    uptime_formatted: str
    symbol: str
    paused: bool
    safe_mode: bool
    snapshot_count: int
    feed_lag_seconds: float | None
    feed_lag_formatted: str | None
    status_color: str
    status_dot: str
    is_feed_stale: bool = False
    active_blocks: list[RiskBlockVM] = field(default_factory=list)
    is_trade_blocked: bool = False


@dataclass
class BalancesVM:
    free_usdt: float
    free_btc: float
    locked_usdt: float
    locked_btc: float
    equity_usdt: float | None
    btc_price: float | None = None
    error: str | None = None


@dataclass
class PositionVM:
    has_position: bool
    symbol: str
    entry_price: float
    quantity: float
    stop_price: float
    target_price: float
    unrealised_pnl: float
    side: str
    entry_time: datetime | None
    lifetime_formatted: str
    pnl_class: str = "text-slate-400"


@dataclass
class MetricsVM:
    daily_pnl: float
    total_pnl: float
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    max_drawdown: float
    daily_pnl_class: str = "text-slate-400"
    total_pnl_class: str = "text-slate-400"


@dataclass
class RiskStatusVM:
    safe_mode: bool
    daily_trades: int
    daily_pnl: float
    consecutive_losses: int
    current_day: str
    # Effective (possibly overridden) limits
    max_daily_loss_pct: float = 3.0
    max_consecutive_losses: int = 3
    max_trades_per_day: int = 10
    min_free_balance_usdt: float = 20.0
    initial_capital_usdt: float = 200.0
    # ENV-file default limits (for display / restore reference)
    default_max_daily_loss_pct: float = 3.0
    default_max_consecutive_losses: int = 3
    default_max_trades_per_day: int = 10
    default_min_free_balance_usdt: float = 20.0
    # Override metadata
    has_overrides: bool = False
    cooldown_active: bool = False
    active_blocks: list[RiskBlockVM] = field(default_factory=list)


@dataclass
class SystemStatusVM:
    ws_connected: bool
    feed_lag_seconds: float | None
    snapshot_count: int
    feed_lag_formatted: str | None = None
    is_feed_stale: bool = False


@dataclass
class TradeVM:
    id: int
    symbol: str
    entry_price: float
    exit_price: float | None
    quantity: float
    net_pnl: float | None
    exit_reason: str | None
    opened_at: str
    closed_at: str | None
    stop_price: float | None = None
    target_price: float | None = None
    explanation: str = ""
    pnl_class: str = "text-slate-400"


@dataclass
class EventVM:
    id: int
    created_at: str
    event_type: str
    description: str | None
    mode: str
    event_class: str = "text-slate-400"


@dataclass
class SignalVM:
    """One signal row — used by both the dashboard situation panel and the Signals page."""

    signal_type: str          # "BUY" | "EXIT" | "HOLD"
    price: float
    timestamp: str            # "HH:MM:SS" for compact display
    explanation_friendly: str
    ema_fast: float | None = None
    ema_slow: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    acted_on: bool = False
    rejected_reason: str | None = None  # Why a BUY was not executed (fee filter, risk, etc.)
    # Extended fields — used on the full Signals page
    id: int = 0
    regime: str | None = None
    atr: float | None = None
    created_at_full: str = ""   # "YYYY-MM-DD HH:MM:SS" for full-page display


@dataclass
class SituationVM:
    """Data bundle for the Current Situation block on the dashboard."""

    signals: list[SignalVM]
    symbol: str
    current_price: float | None
    buffer_ready: bool
    ema_fast_period: int
    ema_slow_period: int
    # Chart data — Python lists; serialised to JSON inside the template
    chart_labels: list[str]                  # e.g. ["14:35", "14:36", …]
    chart_closes: list[float]                # 1m close prices
    chart_ema_fast: list[float | None]
    chart_ema_slow: list[float | None]
    # Sparse marker arrays (null everywhere except the matching candle index)
    chart_open_markers: list[float | None]         # entry price at open candle
    chart_close_profit_markers: list[float | None]  # exit price when PnL ≥ 0
    chart_close_loss_markers: list[float | None]    # exit price when PnL < 0
