"""Connection/config helpers for the agentic-memory SDK."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

from agentic_memory.exceptions import ConfigError


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Resolve the database path from explicit arg or environment/config.

    Resolution order:
      1. Explicit ``db_path`` argument.
      2. ``MEMORY_DB_PATH`` environment variable.
      3. Default from ``_lazy_imports.get_config()``.

    Raises:
        ConfigError: If none of the sources yields a valid path.
    """
    if db_path is not None:
        return Path(db_path)

    env_path = os.environ.get("MEMORY_DB_PATH")
    if env_path:
        return Path(env_path)

    try:
        from infra._lazy_imports import get_config

        cfg = get_config()
        return Path(cfg.db_path)
    except Exception as exc:
        raise ConfigError(
            f"Could not resolve DB path: {exc}. "
            "Pass db_path explicitly or set MEMORY_DB_PATH."
        ) from exc


def get_db_connection(db_path: str | Path, timeout: float = 10.0) -> Any:
    """Get a connection from the pool for *db_path*.

    Returns the connection object; the caller is responsible for
    calling ``safe_close_db()`` when done (or using ``with_connection``).
    """
    from infra._lazy_imports import connection_pool

    return connection_pool.get(str(db_path), timeout=timeout)


def safe_close_db(conn: Any) -> None:
    """Safely return a connection to the pool."""
    from infra._lazy_imports import safe_close_db as _safe_close

    _safe_close(conn)


def resolve_memory_dir() -> Path:
    """Resolve the memory directory path."""
    try:
        from mcp_common import _resolve_memory_dir

        return _resolve_memory_dir()
    except Exception as exc:
        raise ConfigError(f"Could not resolve memory directory: {exc}") from exc


def parse_search_results(raw: Any) -> list[dict[str, Any]]:
    """Normalize search results into a uniform list-of-dicts format.

    Handles string-JSON, dict-with-results, or list inputs.
    """
    import json

    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        raw = json.loads(raw)
    if isinstance(raw, dict):
        results = raw.get("results")
        if results is None:
            results = raw.get("data", [])
        if results is None:
            return []
        return cast(list[dict[str, Any]], results)
    if isinstance(raw, list):
        return raw
    return []
