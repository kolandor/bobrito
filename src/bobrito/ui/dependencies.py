"""FastAPI DI helpers specific to the Web UI module."""

from __future__ import annotations

from bobrito.api.deps import _bot
from bobrito.engine.bot import TradingBot


def get_bot_optional() -> TradingBot | None:
    """Return the bot instance without raising if not initialised."""
    return _bot
