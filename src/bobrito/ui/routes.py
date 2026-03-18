"""Web UI routes: pages, HTMX partials, and action endpoints.

Architecture:
  All write actions go through the existing bot service layer.
  The UI is a client of the system, not an alternative control path.

Route groups:
  - Auth:     GET/POST /login, POST /logout
  - Pages:    GET / (redirect), /dashboard, /trading, /trades, /signals, /system
  - Partials: GET /partials/*  (HTMX polling targets)
  - Actions:  POST /actions/*  (bot control, protected + read-only aware)
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from bobrito.api.deps import get_bot
from bobrito.config.settings import Settings
from bobrito.monitoring.logger import get_logger
from bobrito.persistence.database import get_db_manager
from bobrito.ui.auth import check_credentials
from bobrito.ui.dependencies import get_bot_optional
from bobrito.ui.services import UIService

log = get_logger("ui.routes")

_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _set_flash(request: Request, type_: str, message: str) -> None:
    request.session["flash"] = {"type": type_, "message": message}


def create_ui_router(settings: Settings) -> APIRouter:
    """Factory that builds the UI router bound to the given settings."""
    prefix = settings.web_ui_route_prefix.rstrip("/")
    router = APIRouter(prefix=prefix, tags=["Web UI"])

    def _base_ctx(request: Request, **extra) -> dict:
        flash = request.session.pop("flash", None)
        return {
            "request": request,
            "prefix": prefix,
            "mode": settings.bot_mode.value,
            "is_live": settings.is_live(),
            "readonly": settings.web_ui_readonly,
            "allow_emergency_stop": settings.web_ui_allow_emergency_stop,
            "allow_start_stop": settings.web_ui_allow_start_stop,
            "confirm_live_actions": settings.web_ui_confirm_live_actions,
            "refresh_seconds": settings.web_ui_page_refresh_seconds,
            "flash": flash,
            **extra,
        }

    def _require_auth(request: Request) -> RedirectResponse | None:
        if not request.session.get("authenticated"):
            return RedirectResponse(url=f"{prefix}/login", status_code=302)
        return None

    def _block_readonly(request: Request) -> bool:
        if settings.web_ui_readonly:
            log.warning(f"Readonly action denied for path={request.url.path}")
            _set_flash(request, "error", "Read-only mode — control actions are disabled.")
            return True
        return False

    def _partial_auth_check(request: Request) -> HTMLResponse | None:
        if not request.session.get("authenticated"):
            return HTMLResponse(
                "",
                status_code=401,
                headers={"HX-Redirect": f"{prefix}/login"},
            )
        return None

    # ── Auth ─────────────────────────────────────────────────────────────────

    @router.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if request.session.get("authenticated"):
            return RedirectResponse(url=f"{prefix}/dashboard", status_code=302)
        ctx = {
            "request": request,
            "prefix": prefix,
            "flash": request.session.pop("flash", None),
        }
        return _templates.TemplateResponse("login.html", ctx)

    @router.post("/login")
    async def login_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ):
        if check_credentials(username, password, settings):
            request.session["authenticated"] = True
            request.session["username"] = username
            log.info(f"UI login success: user={username!r}")
            return RedirectResponse(url=f"{prefix}/dashboard", status_code=302)
        log.warning(f"UI login failure: user={username!r}")
        _set_flash(request, "error", "Invalid username or password.")
        return RedirectResponse(url=f"{prefix}/login", status_code=302)

    @router.post("/logout")
    async def logout(request: Request):
        username = request.session.get("username", "unknown")
        request.session.clear()
        log.info(f"UI logout: user={username!r}")
        return RedirectResponse(url=f"{prefix}/login", status_code=302)

    # ── HTML Pages ────────────────────────────────────────────────────────────

    @router.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if redir := _require_auth(request):
            return redir
        return RedirectResponse(url=f"{prefix}/dashboard", status_code=302)

    @router.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if redir := _require_auth(request):
            return redir
        return _templates.TemplateResponse("dashboard.html", _base_ctx(request))

    @router.get("/trading", response_class=HTMLResponse)
    async def trading_page(request: Request):
        if redir := _require_auth(request):
            return redir
        return _templates.TemplateResponse("trading.html", _base_ctx(request))

    @router.get("/trades", response_class=HTMLResponse)
    async def trades_page(request: Request):
        if redir := _require_auth(request):
            return redir
        return _templates.TemplateResponse("trades.html", _base_ctx(request))

    @router.get("/system", response_class=HTMLResponse)
    async def system_page(request: Request):
        if redir := _require_auth(request):
            return redir
        return _templates.TemplateResponse("system.html", _base_ctx(request))

    @router.get("/signals", response_class=HTMLResponse)
    async def signals_page(request: Request):
        if redir := _require_auth(request):
            return redir
        return _templates.TemplateResponse("signals.html", _base_ctx(request))

    @router.get("/signals/cards", response_class=HTMLResponse)
    async def signals_cards(request: Request):
        if err := _partial_auth_check(request):
            return err
        raw_type = request.query_params.get("type", "ALL").upper()
        try:
            offset = int(request.query_params.get("offset", "0"))
        except ValueError:
            offset = 0
        db = get_db_manager()
        bot = get_bot_optional()
        svc = UIService(bot, settings)  # type: ignore[arg-type]
        signals, has_more = await svc.get_signals_batch(
            db, offset=offset, limit=30, signal_type=raw_type
        )
        return _templates.TemplateResponse(
            "partials/signal_cards.html",
            _base_ctx(
                request,
                signals=signals,
                has_more=has_more,
                next_offset=offset + 30,
                signal_type=raw_type,
            ),
        )

    # ── HTMX Partial Routes ───────────────────────────────────────────────────

    @router.get("/partials/dashboard-status", response_class=HTMLResponse)
    async def partial_bot_status(request: Request):
        if err := _partial_auth_check(request):
            return err
        bot = get_bot_optional()
        status_vm = UIService(bot, settings).get_bot_status() if bot else None
        return _templates.TemplateResponse(
            "partials/bot_status_card.html",
            _base_ctx(request, status_vm=status_vm),
        )

    @router.get("/partials/balances", response_class=HTMLResponse)
    async def partial_balances(request: Request):
        if err := _partial_auth_check(request):
            return err
        bot = get_bot_optional()
        balances_vm = await UIService(bot, settings).get_balances() if bot else None
        return _templates.TemplateResponse(
            "partials/balances_card.html",
            _base_ctx(request, balances_vm=balances_vm),
        )

    @router.get("/partials/position", response_class=HTMLResponse)
    async def partial_position(request: Request):
        if err := _partial_auth_check(request):
            return err
        bot = get_bot_optional()
        position_vm = UIService(bot, settings).get_position() if bot else None
        return _templates.TemplateResponse(
            "partials/position_card.html",
            _base_ctx(request, position_vm=position_vm),
        )

    @router.get("/partials/metrics", response_class=HTMLResponse)
    async def partial_metrics(request: Request):
        if err := _partial_auth_check(request):
            return err
        bot = get_bot_optional()
        metrics_vm = UIService(bot, settings).get_metrics() if bot else None
        return _templates.TemplateResponse(
            "partials/metrics_card.html",
            _base_ctx(request, metrics_vm=metrics_vm),
        )

    @router.get("/partials/risk", response_class=HTMLResponse)
    async def partial_risk(request: Request):
        if err := _partial_auth_check(request):
            return err
        bot = get_bot_optional()
        risk_vm = UIService(bot, settings).get_risk_status() if bot else None
        return _templates.TemplateResponse(
            "partials/risk_status_card.html",
            _base_ctx(request, risk_vm=risk_vm),
        )

    @router.get("/partials/system-status", response_class=HTMLResponse)
    async def partial_system_status(request: Request):
        if err := _partial_auth_check(request):
            return err
        bot = get_bot_optional()
        system_vm = UIService(bot, settings).get_system_status() if bot else None
        return _templates.TemplateResponse(
            "partials/system_status_card.html",
            _base_ctx(request, system_vm=system_vm),
        )

    @router.get("/partials/trades-table", response_class=HTMLResponse)
    async def partial_trades_table(request: Request):
        if err := _partial_auth_check(request):
            return err
        bot = get_bot_optional()
        trades = []
        if bot:
            try:
                db = get_db_manager()
                trades = await UIService(bot, settings).get_recent_trades(db)
            except Exception as exc:
                log.debug(f"partial_trades_table error: {exc}")
        return _templates.TemplateResponse(
            "partials/trades_table.html",
            _base_ctx(request, trades=trades),
        )

    @router.get("/partials/events-table", response_class=HTMLResponse)
    async def partial_events_table(request: Request):
        if err := _partial_auth_check(request):
            return err
        bot = get_bot_optional()
        events = []
        if bot:
            try:
                db = get_db_manager()
                events = await UIService(bot, settings).get_recent_events(db)
            except Exception as exc:
                log.debug(f"partial_events_table error: {exc}")
        return _templates.TemplateResponse(
            "partials/events_table.html",
            _base_ctx(request, events=events),
        )

    @router.get("/partials/situation", response_class=HTMLResponse)
    async def partial_situation(request: Request):
        if err := _partial_auth_check(request):
            return err
        bot = get_bot_optional()
        situation_vm = None
        if bot:
            try:
                db = get_db_manager()
                situation_vm = await UIService(bot, settings).get_situation(db)
            except Exception as exc:
                log.debug(f"partial_situation error: {exc}")
        return _templates.TemplateResponse(
            "partials/situation.html",
            _base_ctx(request, situation_vm=situation_vm),
        )

    @router.get("/partials/control-buttons", response_class=HTMLResponse)
    async def partial_control_buttons(request: Request):
        if err := _partial_auth_check(request):
            return err
        bot = get_bot_optional()
        bot_status = bot.status.value if bot else "stopped"
        return _templates.TemplateResponse(
            "partials/control_buttons.html",
            _base_ctx(request, bot_status=bot_status),
        )

    @router.get("/partials/risk-limiter", response_class=HTMLResponse)
    async def partial_risk_limiter(request: Request):
        if err := _partial_auth_check(request):
            return err
        bot = get_bot_optional()
        risk_vm = UIService(bot, settings).get_risk_status() if bot else None
        return _templates.TemplateResponse(
            "partials/risk_limiter_controls.html",
            _base_ctx(request, risk_vm=risk_vm),
        )

    # ── Action Routes ─────────────────────────────────────────────────────────

    @router.post("/actions/start")
    async def action_start(request: Request):
        if redir := _require_auth(request):
            return redir
        if _block_readonly(request):
            return RedirectResponse(url=f"{prefix}/dashboard", status_code=302)
        if not settings.web_ui_allow_start_stop:
            _set_flash(request, "error", "Start/stop actions are disabled in configuration.")
            return RedirectResponse(url=f"{prefix}/dashboard", status_code=302)
        try:
            bot = get_bot()
            if bot.status.value in ("running", "starting"):
                _set_flash(request, "info", f"Bot is already {bot.status.value}.")
            else:
                await bot.start()
                _set_flash(request, "success", "Bot started successfully.")
                log.info("UI audit: action=start")
        except Exception as exc:
            log.exception("UI action start failed")
            _set_flash(request, "error", f"Failed to start bot: {exc}")
        return RedirectResponse(url=f"{prefix}/dashboard", status_code=302)

    @router.post("/actions/stop")
    async def action_stop(request: Request):
        if redir := _require_auth(request):
            return redir
        if _block_readonly(request):
            return RedirectResponse(url=f"{prefix}/dashboard", status_code=302)
        if not settings.web_ui_allow_start_stop:
            _set_flash(request, "error", "Start/stop actions are disabled in configuration.")
            return RedirectResponse(url=f"{prefix}/dashboard", status_code=302)
        try:
            bot = get_bot()
            if bot.status.value == "stopped":
                _set_flash(request, "info", "Bot is already stopped.")
            else:
                await bot.stop()
                _set_flash(request, "success", "Bot stopped.")
                log.info("UI audit: action=stop")
        except Exception as exc:
            log.exception("UI action stop failed")
            _set_flash(request, "error", f"Failed to stop bot: {exc}")
        return RedirectResponse(url=f"{prefix}/dashboard", status_code=302)

    @router.post("/actions/pause")
    async def action_pause(request: Request):
        if redir := _require_auth(request):
            return redir
        if _block_readonly(request):
            return RedirectResponse(url=f"{prefix}/dashboard", status_code=302)
        if not settings.web_ui_allow_start_stop:
            _set_flash(request, "error", "Pause/resume actions are disabled in configuration.")
            return RedirectResponse(url=f"{prefix}/dashboard", status_code=302)
        try:
            bot = get_bot()
            if bot.status.value != "running":
                _set_flash(
                    request, "error", f"Bot must be running to pause (current: {bot.status.value})."
                )
            else:
                bot.pause()
                _set_flash(
                    request, "success", "Bot paused — exits still monitored, no new entries."
                )
                log.info("UI audit: action=pause")
        except Exception as exc:
            log.exception("UI action pause failed")
            _set_flash(request, "error", f"Failed to pause bot: {exc}")
        return RedirectResponse(url=f"{prefix}/dashboard", status_code=302)

    @router.post("/actions/resume")
    async def action_resume(request: Request):
        if redir := _require_auth(request):
            return redir
        if _block_readonly(request):
            return RedirectResponse(url=f"{prefix}/dashboard", status_code=302)
        if not settings.web_ui_allow_start_stop:
            _set_flash(request, "error", "Pause/resume actions are disabled in configuration.")
            return RedirectResponse(url=f"{prefix}/dashboard", status_code=302)
        try:
            bot = get_bot()
            if bot.status.value != "paused":
                _set_flash(
                    request, "error", f"Bot must be paused to resume (current: {bot.status.value})."
                )
            else:
                bot.resume()
                _set_flash(request, "success", "Bot resumed.")
                log.info("UI audit: action=resume")
        except Exception as exc:
            log.exception("UI action resume failed")
            _set_flash(request, "error", f"Failed to resume bot: {exc}")
        return RedirectResponse(url=f"{prefix}/dashboard", status_code=302)

    @router.post("/actions/emergency-stop")
    async def action_emergency_stop(request: Request):
        if redir := _require_auth(request):
            return redir
        if _block_readonly(request):
            return RedirectResponse(url=f"{prefix}/dashboard", status_code=302)
        if not settings.web_ui_allow_emergency_stop:
            _set_flash(request, "error", "Emergency stop is disabled in configuration.")
            return RedirectResponse(url=f"{prefix}/dashboard", status_code=302)
        try:
            bot = get_bot()
            await bot.emergency_stop()
            _set_flash(request, "success", "Emergency stop executed. Bot halted.")
            log.warning("UI audit: action=emergency_stop")
        except Exception as exc:
            log.exception("UI action emergency_stop failed")
            _set_flash(request, "error", f"Emergency stop failed: {exc}")
        return RedirectResponse(url=f"{prefix}/dashboard", status_code=302)

    # ── Risk Limiter Actions ───────────────────────────────────────────────────

    @router.post("/actions/risk/reset-cooldown")
    async def action_risk_reset_cooldown(request: Request):
        if redir := _require_auth(request):
            return redir
        if _block_readonly(request):
            return RedirectResponse(url=f"{prefix}/system", status_code=302)
        try:
            bot = get_bot()
            await bot.get_risk().reset_cooldown()
            _set_flash(
                request,
                "success",
                "Post-loss cooldown and consecutive loss streak reset. New entries are now unblocked.",
            )
            log.warning("UI audit: action=risk_reset_cooldown")
        except Exception as exc:
            log.exception("UI action risk_reset_cooldown failed")
            _set_flash(request, "error", f"Reset cooldown failed: {exc}")
        return RedirectResponse(url=f"{prefix}/system", status_code=302)

    @router.post("/actions/risk/update-limits")
    async def action_risk_update_limits(
        request: Request,
        max_consecutive_losses: str = Form(""),
        max_daily_loss_pct: str = Form(""),
        min_free_balance_usdt: str = Form(""),
        max_trades_per_day: str = Form(""),
    ):
        if redir := _require_auth(request):
            return redir
        if _block_readonly(request):
            return RedirectResponse(url=f"{prefix}/system", status_code=302)
        try:
            bot = get_bot()
            risk = bot.get_risk()
            changed: list[str] = []

            if max_consecutive_losses.strip():
                val = int(max_consecutive_losses.strip())
                if val < 1:
                    raise ValueError("Consecutive losses limit must be ≥ 1.")
                risk.set_max_consecutive_losses(val)
                changed.append(f"consecutive losses → {val}")

            if max_daily_loss_pct.strip():
                val_f = float(max_daily_loss_pct.strip())
                if val_f <= 0 or val_f > 20:
                    raise ValueError("Daily PnL limit must be between 0.1% and 20%.")
                risk.set_max_daily_loss_pct(val_f)
                changed.append(f"daily PnL limit → {val_f}%")

            if min_free_balance_usdt.strip():
                val_f = float(min_free_balance_usdt.strip())
                if val_f < 0:
                    raise ValueError("Minimum free balance must be ≥ 0.")
                risk.set_min_free_balance_usdt(val_f)
                changed.append(f"min free balance → {val_f} USDT")

            if max_trades_per_day.strip():
                val = int(max_trades_per_day.strip())
                if val < 1:
                    raise ValueError("Daily trades limit must be ≥ 1.")
                risk.set_max_trades_per_day(val)
                changed.append(f"daily trades → {val}")

            if changed:
                _set_flash(
                    request,
                    "success",
                    "Risk limits updated: "
                    + ", ".join(changed)
                    + ". Revert automatically at midnight.",
                )
                log.warning(f"UI audit: action=risk_update_limits changes={changed}")
            else:
                _set_flash(request, "info", "No limit values were changed (all fields were empty).")
        except ValueError as exc:
            _set_flash(request, "error", f"Invalid value: {exc}")
        except Exception as exc:
            log.exception("UI action risk_update_limits failed")
            _set_flash(request, "error", f"Failed to update limits: {exc}")
        return RedirectResponse(url=f"{prefix}/system", status_code=302)

    @router.post("/actions/risk/restore-defaults")
    async def action_risk_restore_defaults(request: Request):
        if redir := _require_auth(request):
            return redir
        if _block_readonly(request):
            return RedirectResponse(url=f"{prefix}/system", status_code=302)
        try:
            bot = get_bot()
            bot.get_risk().restore_defaults()
            _set_flash(
                request,
                "success",
                "Risk limits restored to ENV-file defaults.",
            )
            log.info("UI audit: action=risk_restore_defaults")
        except Exception as exc:
            log.exception("UI action risk_restore_defaults failed")
            _set_flash(request, "error", f"Failed to restore defaults: {exc}")
        return RedirectResponse(url=f"{prefix}/system", status_code=302)

    return router
