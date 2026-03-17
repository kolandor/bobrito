"""Integration tests for the Web UI routes.

Tests cover:
- UI disabled: /ui/* routes are not registered
- UI enabled: login/logout flow, route protection
- Dashboard rendering (authenticated)
- HTMX partial rendering
- Action routes invoke bot service layer
- Read-only mode blocks control actions
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _make_mock_bot(status: str = "running"):
    bot = MagicMock()
    bot.status.value = status
    bot.get_status_dict.return_value = {
        "status": status,
        "mode": "paper",
        "symbol": "BTCUSDT",
        "uptime_seconds": 100.0,
        "paused": False,
        "snapshot_count": 5,
        "safe_mode": False,
        "feed_lag_seconds": 0.5,
        "has_open_position": False,
    }
    portfolio = MagicMock()
    portfolio.stats.return_value = {
        "total_trades": 0, "wins": 0, "losses": 0,
        "win_rate_pct": 0.0, "total_pnl_usdt": 0.0, "max_drawdown_pct": 0.0,
    }
    portfolio.get_open_position.return_value = None
    bot.get_portfolio.return_value = portfolio
    risk = MagicMock()
    risk.daily_pnl = 0.0
    risk.state_dict.return_value = {
        "safe_mode": False, "daily_trades": 0, "daily_pnl": 0.0,
        "consecutive_losses": 0, "current_day": "2026-03-15",
        "limits": {
            "max_consecutive_losses": 3,
            "max_daily_loss_pct": 3.0,
            "min_free_balance_usdt": 50.0,
            "max_trades_per_day": 10,
        },
        "defaults": {
            "max_consecutive_losses": 3,
            "max_daily_loss_pct": 3.0,
            "min_free_balance_usdt": 50.0,
            "max_trades_per_day": 10,
        },
        "has_overrides": False,
    }
    bot.get_risk.return_value = risk
    bot.get_last_snapshot.return_value = None
    bot._broker = None
    return bot


def _make_settings(
    web_ui_enabled: bool = True,
    web_ui_readonly: bool = False,
    web_ui_allow_start_stop: bool = True,
    web_ui_allow_emergency_stop: bool = True,
    bot_mode: str = "paper",
):
    s = MagicMock()
    s.web_ui_enabled = web_ui_enabled
    s.web_ui_route_prefix = "/ui"
    s.web_ui_readonly = web_ui_readonly
    s.web_ui_username = "admin"
    s.web_ui_password = "testpass"
    s.web_ui_session_secret = "test_secret_key_that_is_long_enough_32chars"
    s.web_ui_page_refresh_seconds = 5
    s.web_ui_allow_emergency_stop = web_ui_allow_emergency_stop
    s.web_ui_allow_start_stop = web_ui_allow_start_stop
    s.web_ui_confirm_live_actions = True
    s.web_ui_show_debug_blocks = False
    s.web_ui_show_raw_metrics = False
    s.bot_mode.value = bot_mode
    s.is_live.return_value = bot_mode == "live"
    s.max_daily_loss_pct = 3.0
    s.max_consecutive_losses = 3
    s.max_trades_per_day = 10
    # Other settings needed by app
    s.log_level = "DEBUG"
    s.log_file = "./logs/test.log"
    s.database_url = "sqlite+aiosqlite:///:memory:"
    s.metrics_port = 9091
    return s


def _build_ui_app(settings, bot):
    """Build a minimal FastAPI app with just the UI router (no lifespan)."""
    from fastapi import FastAPI
    from starlette.middleware.sessions import SessionMiddleware

    from bobrito.ui.routes import create_ui_router

    app = FastAPI()
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.web_ui_session_secret,
        session_cookie="bobrito_session",
        max_age=3600,
        https_only=False,
        same_site="lax",
    )
    ui_router = create_ui_router(settings)
    app.include_router(ui_router)
    return app


class TestUIDisabled:
    """When WEB_UI_ENABLED=false, /ui/* routes must not exist."""

    def test_ui_routes_not_registered_when_disabled(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient as SyncClient

        app = FastAPI()
        # Deliberately do NOT add the UI router
        client = SyncClient(app)
        resp = client.get("/ui/dashboard")
        assert resp.status_code == 404

    def test_ui_routes_registered_when_enabled(self):
        settings = _make_settings(web_ui_enabled=True)
        bot = _make_mock_bot()
        app = _build_ui_app(settings, bot)
        client = TestClient(app)
        # Unauthenticated access → redirect to login
        resp = client.get("/ui/dashboard", follow_redirects=False)
        assert resp.status_code == 302
        assert "/ui/login" in resp.headers["location"]


class TestLoginLogout:
    def setup_method(self):
        self.settings = _make_settings()
        self.bot = _make_mock_bot()
        self.app = _build_ui_app(self.settings, self.bot)
        self.client = TestClient(self.app, follow_redirects=False)

    def test_login_page_accessible(self):
        resp = self.client.get("/ui/login")
        assert resp.status_code == 200
        assert b"Sign in" in resp.content

    def test_login_already_authenticated_redirects(self):
        # Login first
        self.client.post("/ui/login", data={"username": "admin", "password": "testpass"})
        resp = self.client.get("/ui/login")
        assert resp.status_code == 302
        assert "/ui/dashboard" in resp.headers["location"]

    def test_login_correct_credentials(self):
        resp = self.client.post(
            "/ui/login", data={"username": "admin", "password": "testpass"}
        )
        assert resp.status_code == 302
        assert "/ui/dashboard" in resp.headers["location"]

    def test_login_wrong_password(self):
        resp = self.client.post(
            "/ui/login", data={"username": "admin", "password": "wrong"}
        )
        assert resp.status_code == 302
        assert "/ui/login" in resp.headers["location"]

    def test_login_wrong_username(self):
        resp = self.client.post(
            "/ui/login", data={"username": "hacker", "password": "testpass"}
        )
        assert resp.status_code == 302
        assert "/ui/login" in resp.headers["location"]

    def test_logout_clears_session(self):
        # Login first
        self.client.post("/ui/login", data={"username": "admin", "password": "testpass"})
        # Logout
        resp = self.client.post("/ui/logout")
        assert resp.status_code == 302
        # After logout, dashboard should redirect to login
        resp2 = self.client.get("/ui/dashboard")
        assert resp2.status_code == 302
        assert "/ui/login" in resp2.headers["location"]


class TestRouteProtection:
    def setup_method(self):
        self.settings = _make_settings()
        self.bot = _make_mock_bot()
        self.app = _build_ui_app(self.settings, self.bot)
        self.client = TestClient(self.app, follow_redirects=False)

    def _login(self):
        self.client.post("/ui/login", data={"username": "admin", "password": "testpass"})

    def test_unauthenticated_dashboard_redirects_to_login(self):
        resp = self.client.get("/ui/dashboard")
        assert resp.status_code == 302
        assert "/ui/login" in resp.headers["location"]

    def test_unauthenticated_trading_redirects_to_login(self):
        resp = self.client.get("/ui/trading")
        assert resp.status_code == 302
        assert "/ui/login" in resp.headers.get("location", "")

    def test_unauthenticated_action_redirects_to_login(self):
        resp = self.client.post("/ui/actions/pause")
        assert resp.status_code == 302
        assert "/ui/login" in resp.headers["location"]

    def test_authenticated_dashboard_returns_200(self):
        self._login()
        with patch("bobrito.ui.routes.get_bot_optional", return_value=None):
            resp = self.client.get("/ui/dashboard")
        assert resp.status_code == 200

    def test_index_redirects_to_dashboard_when_authed(self):
        self._login()
        resp = self.client.get("/ui/")
        assert resp.status_code == 302
        assert "/ui/dashboard" in resp.headers["location"]


class TestHTMXPartials:
    def setup_method(self):
        self.settings = _make_settings()
        self.bot = _make_mock_bot()
        self.app = _build_ui_app(self.settings, self.bot)
        self.client = TestClient(self.app, follow_redirects=False)
        # Authenticate
        self.client.post("/ui/login", data={"username": "admin", "password": "testpass"})

    def test_bot_status_partial_returns_html(self):
        with patch("bobrito.ui.routes.get_bot_optional", return_value=self.bot):
            resp = self.client.get("/ui/partials/dashboard-status")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_balances_partial_returns_html(self):
        with patch("bobrito.ui.routes.get_bot_optional", return_value=self.bot):
            resp = self.client.get("/ui/partials/balances")
        assert resp.status_code == 200

    def test_position_partial_returns_html(self):
        with patch("bobrito.ui.routes.get_bot_optional", return_value=self.bot):
            resp = self.client.get("/ui/partials/position")
        assert resp.status_code == 200

    def test_metrics_partial_returns_html(self):
        with patch("bobrito.ui.routes.get_bot_optional", return_value=self.bot):
            resp = self.client.get("/ui/partials/metrics")
        assert resp.status_code == 200

    def test_risk_partial_returns_html(self):
        with patch("bobrito.ui.routes.get_bot_optional", return_value=self.bot):
            resp = self.client.get("/ui/partials/risk")
        assert resp.status_code == 200

    def test_system_partial_returns_html(self):
        with patch("bobrito.ui.routes.get_bot_optional", return_value=self.bot):
            resp = self.client.get("/ui/partials/system-status")
        assert resp.status_code == 200

    def test_partial_unauthenticated_returns_401(self):
        # New client with no session
        from fastapi.testclient import TestClient as FreshClient

        fresh = FreshClient(self.app, follow_redirects=False)
        resp = fresh.get("/ui/partials/dashboard-status")
        assert resp.status_code == 401
        assert "HX-Redirect" in resp.headers


class TestActionRoutes:
    def setup_method(self):
        self.settings = _make_settings()
        self.bot = _make_mock_bot()
        self.app = _build_ui_app(self.settings, self.bot)
        self.client = TestClient(self.app, follow_redirects=False)
        self.client.post("/ui/login", data={"username": "admin", "password": "testpass"})

    def test_pause_calls_bot_pause(self):
        self.bot.status.value = "running"
        with patch("bobrito.ui.routes.get_bot", return_value=self.bot):
            resp = self.client.post("/ui/actions/pause")
        assert resp.status_code == 302
        self.bot.pause.assert_called_once()

    def test_resume_calls_bot_resume(self):
        self.bot.status.value = "paused"
        with patch("bobrito.ui.routes.get_bot", return_value=self.bot):
            resp = self.client.post("/ui/actions/resume")
        assert resp.status_code == 302
        self.bot.resume.assert_called_once()

    def test_stop_calls_bot_stop(self):
        self.bot.status.value = "running"
        self.bot.stop = AsyncMock()
        with patch("bobrito.ui.routes.get_bot", return_value=self.bot):
            resp = self.client.post("/ui/actions/stop")
        assert resp.status_code == 302
        self.bot.stop.assert_called_once()

    def test_start_calls_bot_start(self):
        self.bot.status.value = "stopped"
        self.bot.start = AsyncMock()
        with patch("bobrito.ui.routes.get_bot", return_value=self.bot):
            resp = self.client.post("/ui/actions/start")
        assert resp.status_code == 302
        self.bot.start.assert_called_once()

    def test_emergency_stop_calls_bot_emergency_stop(self):
        self.bot.emergency_stop = AsyncMock()
        with patch("bobrito.ui.routes.get_bot", return_value=self.bot):
            resp = self.client.post("/ui/actions/emergency-stop")
        assert resp.status_code == 302
        self.bot.emergency_stop.assert_called_once()

    def test_all_actions_redirect_to_dashboard(self):
        self.bot.status.value = "running"
        self.bot.pause.return_value = None
        with patch("bobrito.ui.routes.get_bot", return_value=self.bot):
            resp = self.client.post("/ui/actions/pause")
        assert "/ui/dashboard" in resp.headers["location"]


class TestReadOnlyMode:
    def setup_method(self):
        self.settings = _make_settings(web_ui_readonly=True)
        self.bot = _make_mock_bot()
        self.app = _build_ui_app(self.settings, self.bot)
        self.client = TestClient(self.app, follow_redirects=False)
        self.client.post("/ui/login", data={"username": "admin", "password": "testpass"})

    def test_start_blocked_in_readonly(self):
        with patch("bobrito.ui.routes.get_bot", return_value=self.bot):
            resp = self.client.post("/ui/actions/start")
        assert resp.status_code == 302
        self.bot.start.assert_not_called()

    def test_pause_blocked_in_readonly(self):
        with patch("bobrito.ui.routes.get_bot", return_value=self.bot):
            resp = self.client.post("/ui/actions/pause")
        assert resp.status_code == 302
        self.bot.pause.assert_not_called()

    def test_emergency_stop_blocked_in_readonly(self):
        with patch("bobrito.ui.routes.get_bot", return_value=self.bot):
            resp = self.client.post("/ui/actions/emergency-stop")
        assert resp.status_code == 302
        self.bot.emergency_stop.assert_not_called()

    def test_dashboard_still_accessible_in_readonly(self):
        with patch("bobrito.ui.routes.get_bot_optional", return_value=None):
            resp = self.client.get("/ui/dashboard")
        assert resp.status_code == 200
