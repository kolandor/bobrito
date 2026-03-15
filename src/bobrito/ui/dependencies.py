"""FastAPI DI helpers specific to the Web UI module."""

from __future__ import annotations

import bobrito.api.deps as _api_deps
from bobrito.engine.bot import TradingBot


def get_bot_optional() -> TradingBot | None:
    """Return the live bot instance, or None if not yet initialised.

    Accesses deps._bot through the module reference so the lookup always
    reflects the value set by set_bot() during application startup,
    rather than the None that existed at import time.
    """
    return _api_deps._bot
