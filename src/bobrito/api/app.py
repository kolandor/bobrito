"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from bobrito.api.deps import set_bot
from bobrito.api.routes import bot, health, trading
from bobrito.config.settings import get_settings
from bobrito.engine.bot import TradingBot
from bobrito.monitoring.logger import get_logger, setup_logging
from bobrito.monitoring.metrics import MetricsCollector
from bobrito.persistence.database import init_db_manager

log = get_logger("api.app")

_bot_instance: TradingBot | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown."""
    global _bot_instance
    settings = get_settings()
    setup_logging(level=settings.log_level, log_file=settings.log_file)

    log.info(f"Bobrito v1 starting | mode={settings.bot_mode.value}")

    # Database
    db = init_db_manager(settings.database_url)
    await db.init()
    log.info("Database initialised")

    # Bot (auto-start in configured mode)
    _bot_instance = TradingBot(settings=settings, db=db)
    set_bot(_bot_instance)
    app.state.bot = _bot_instance
    app.state.db = db

    # Start Prometheus metrics server
    try:
        MetricsCollector.start_server(port=settings.metrics_port)
        log.info(f"Prometheus metrics on :{settings.metrics_port}")
    except Exception:
        log.warning("Prometheus server could not start (port may be in use)")

    # Auto-start bot
    await _bot_instance.start()

    yield

    # Shutdown
    log.info("Shutting down…")
    if _bot_instance.status.value not in ("stopped", "stopping"):
        await _bot_instance.stop()
    await db.close()
    log.info("Shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Bobrito Trading Bot API",
        description="Automated BTC/USDT Spot Trading Bot",
        version="1.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Optional Web UI ───────────────────────────────────────────────────────
    if settings.web_ui_enabled:
        from starlette.middleware.sessions import SessionMiddleware

        from bobrito.ui.routes import create_ui_router

        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.web_ui_session_secret,
            session_cookie="bobrito_session",
            max_age=3600 * 8,  # 8-hour sessions
            https_only=False,  # set True behind HTTPS proxy
            same_site="lax",
        )

        ui_router = create_ui_router(settings)
        app.include_router(ui_router)

        static_dir = Path(__file__).parent.parent / "ui" / "static"
        prefix = settings.web_ui_route_prefix.rstrip("/")
        app.mount(f"{prefix}/static", StaticFiles(directory=str(static_dir)), name="ui_static")

        log.info(
            f"Web UI enabled | prefix={settings.web_ui_route_prefix} "
            f"readonly={settings.web_ui_readonly}"
        )
    else:
        log.info("Web UI disabled (WEB_UI_ENABLED=false)")

    # ── Core API Routers ──────────────────────────────────────────────────────
    app.include_router(health.router)
    app.include_router(bot.router)
    app.include_router(trading.router)

    @app.exception_handler(Exception)
    async def global_exception_handler(request, exc):
        log.exception(f"Unhandled exception: {exc}")
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    return app
