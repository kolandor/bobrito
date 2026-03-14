"""FastAPI dependency injection helpers."""

from __future__ import annotations

from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from bobrito.config.settings import get_settings
from bobrito.engine.bot import TradingBot

_bearer = HTTPBearer(auto_error=False)

# Module-level reference set at startup
_bot: TradingBot | None = None


def set_bot(bot: TradingBot) -> None:
    global _bot
    _bot = bot


def get_bot() -> TradingBot:
    if _bot is None:
        raise HTTPException(status_code=503, detail="Bot not initialised")
    return _bot


def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> None:
    settings = get_settings()
    expected = settings.api_secret_key
    if not credentials or credentials.credentials != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API token",
        )
