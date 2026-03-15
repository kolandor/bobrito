"""UI session-based authentication helpers."""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bobrito.config.settings import Settings


def check_credentials(username: str, password: str, settings: "Settings | None" = None) -> bool:
    """Constant-time comparison to prevent timing-based attacks.

    Pass *settings* explicitly from the route closure so the function is
    testable without monkey-patching the global lru_cache.
    """
    if settings is None:
        from bobrito.config.settings import get_settings

        settings = get_settings()
    user_ok = hmac.compare_digest(
        username.encode("utf-8"),
        settings.web_ui_username.encode("utf-8"),
    )
    pass_ok = hmac.compare_digest(
        password.encode("utf-8"),
        settings.web_ui_password.encode("utf-8"),
    )
    return user_ok and pass_ok
