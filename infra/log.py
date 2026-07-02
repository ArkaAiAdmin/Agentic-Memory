"""Centralized logging setup for agentic-memory cron jobs and scripts."""

from __future__ import annotations

import logging
import os
from typing import Any

from infra.memory_config import configure_logging


def setup_logging(
    name: str,
    level: int | str | None = None,
    fmt: str | None = None,
    **kwargs: Any,
) -> logging.Logger:
    """Setup logging globally (if not already done) and return a named logger.

    Ensures configure_logging() is called, but permits local overrides for
    format and log level for standalone cron/script executions.
    """
    if not logging.getLogger().handlers:
        log_format = os.environ.get("LOG_FORMAT", "text")
        if log_format == "json":
            configure_logging()
        else:
            actual_level = level if level is not None else os.environ.get("LOG_LEVEL", "INFO")
            actual_fmt = fmt if fmt is not None else "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
            logging.basicConfig(level=actual_level, format=actual_fmt, **kwargs)

    logger = logging.getLogger(name)
    if level is not None:
        logger.setLevel(level)
    return logger
