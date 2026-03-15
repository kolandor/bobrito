"""View model dataclasses used as template context for the Web UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


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
    max_daily_loss_pct: float = 3.0
    max_consecutive_losses: int = 3
    max_trades_per_day: int = 10


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
    pnl_class: str = "text-slate-400"


@dataclass
class EventVM:
    id: int
    created_at: str
    event_type: str
    description: str | None
    mode: str
    event_class: str = "text-slate-400"
