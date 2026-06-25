"""Exception-safe callable wrapper for graceful degradation.

Extracted from memory_common.py during the 6-module refactor.

Provides:
  * ``safe_call(func, *args, fallback=None, log_level=WARNING, err_label='operation', **kwargs)``.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

__all__ = ['safe_call']


def safe_call(
    func: Callable[..., Any],
    *args,
    fallback: Any = None,
    log_level: int = logging.WARNING,
    err_label: str = 'operation',
    raise_on: tuple[type[BaseException], ...] | None = None,
    **kwargs,
) -> Any:
    """Call ``func(*args, **kwargs)`` and return its result, or ``fallback`` on exception.

    M1 fix: replaces the repeated

        try:
            return some_function(...)
        except Exception as e:
            logger.warning(...)
            return <sentinel>

    pattern that lived in 30+ places across memory_mcp.py. The
    ``err_label`` appears in the warning so the log line is still
    attributable to a specific call site. The exception is NOT
    re-raised — this helper is for "graceful degradation" sites only.
    For places that need the exception to propagate, use a plain
    try/except so the type checker can follow control flow.

    Args:
        func: Callable to invoke.
        *args: Positional args forwarded to ``func``.
        fallback: Value to return if ``func`` raises.
        log_level: logging level for the warning (default WARNING).
        err_label: Short label included in the warning, e.g. "read db".
        raise_on: Exception types that should propagate (not be caught).
        **kwargs: Keyword args forwarded to ``func``.

    Returns:
        The result of ``func(*args, **kwargs)``, or ``fallback`` on exception.
    """
    logger = logging.getLogger(__name__)
    try:
        return func(*args, **kwargs)
    except Exception as e:
        if raise_on and isinstance(e, raise_on):
            raise
        logger.log(log_level, 'safe_call[%s] failed: %s', err_label, e)
        return fallback
