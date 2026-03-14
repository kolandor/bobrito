"""Structured logging via Loguru.

Every log record is emitted as structured JSON so it can be ingested
by log aggregation systems. A human-readable format goes to stderr.
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    logger.remove()

    fmt = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{extra[module]}</cyan> | "
        "{message}"
    )

    logger.add(
        sys.stderr,
        level=level,
        format=fmt,
        colorize=True,
        filter=lambda r: True,
    )

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_file,
            level=level,
            rotation="100 MB",
            retention="30 days",
            compression="gz",
            serialize=True,   # JSON output to file
            enqueue=True,
        )

    logger.info("Logging initialised", module="monitoring")


def get_logger(module: str):  # noqa: ANN201
    """Return a logger bound with a module name in every record."""
    return logger.bind(module=module)
