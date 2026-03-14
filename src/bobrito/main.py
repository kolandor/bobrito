"""Bobrito entry point.

Usage:
    uvicorn bobrito.main:app --host 0.0.0.0 --port 8080
    python -m bobrito.main
"""

from __future__ import annotations

import uvicorn

from bobrito.api.app import create_app
from bobrito.config.settings import get_settings

app = create_app()


def cli() -> None:
    settings = get_settings()
    uvicorn.run(
        "bobrito.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    cli()
