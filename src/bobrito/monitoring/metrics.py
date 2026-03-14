"""Prometheus metrics definitions."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, start_http_server


class MetricsCollector:
    """Singleton-style Prometheus metrics registry."""

    # ── Technical ─────────────────────────────────────────────────────────
    uptime_seconds = Gauge("bobrito_uptime_seconds", "Bot uptime in seconds")
    ws_connected = Gauge("bobrito_ws_connected", "WebSocket connection state (1=up, 0=down)")
    api_errors_total = Counter("bobrito_api_errors_total", "Total API errors", ["endpoint"])
    order_rejections_total = Counter(
        "bobrito_order_rejections_total", "Total order rejections", ["reason"]
    )
    order_latency_seconds = Histogram(
        "bobrito_order_latency_seconds", "Order placement latency"
    )
    candle_feed_lag_seconds = Gauge(
        "bobrito_candle_feed_lag_seconds", "Seconds since last candle update"
    )

    # ── Trading ────────────────────────────────────────────────────────────
    trades_total = Counter("bobrito_trades_total", "Total trades executed", ["side", "mode"])
    wins_total = Counter("bobrito_wins_total", "Winning trades", ["mode"])
    losses_total = Counter("bobrito_losses_total", "Losing trades", ["mode"])
    pnl_realised_usdt = Gauge("bobrito_pnl_realised_usdt", "Total realised PnL in USDT")
    max_drawdown_pct = Gauge("bobrito_max_drawdown_pct", "Maximum drawdown percentage")
    daily_profit_usdt = Gauge("bobrito_daily_profit_usdt", "Daily PnL in USDT")
    equity_usdt = Gauge("bobrito_equity_usdt", "Current equity in USDT")
    open_position = Gauge("bobrito_open_position", "1 if a position is open, 0 otherwise")

    # ── Risk ──────────────────────────────────────────────────────────────
    risk_events_total = Counter(
        "bobrito_risk_events_total", "Risk management events triggered", ["event_type"]
    )

    @classmethod
    def start_server(cls, port: int = 9090) -> None:
        start_http_server(port)
