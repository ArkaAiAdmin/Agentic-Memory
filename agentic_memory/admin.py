"""Admin SDK — health, circuit breaker, and system status."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentic_memory.utils import resolve_db_path


class Admin:
    """System administration operations.

    Provides health checks, circuit-breaker introspection, and other
    operator-facing functionality missing from the domain-specific SDK
    classes.

    Args:
        db_path: Path to the memory database. If None, resolved from
            environment or config.
    """

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = resolve_db_path(db_path)

    def health(self, db_path: str | Path | None = None) -> dict[str, Any]:
        """Return per-table row counts and staleness flags.

        Delegates to the same ``health_check`` used by the backfill
        pipeline.

        Args:
            db_path: Optional override DB path. Defaults to the instance
                path.

        Returns:
            Dict with keys ``db_path``, ``tables`` (per-table stats),
            ``all_healthy``, ``stale_count``.
        """
        from backfill.orchestrator import health_check as _health_check

        result = _health_check(db_path or self.db_path)
        return dict(result)

    def circuit_breaker_status(
        self,
        limit: int = 20,
        since_ts: float | None = None,
    ) -> dict[str, Any]:
        """Return auto-save circuit-breaker open/close history.

        Surfaces events persisted to ``memory_audit_log`` so operators
        can see breaker state transitions across process restarts.

        Args:
            limit: Maximum events to return (1..200).
            since_ts: Optional Unix-epoch lower bound.

        Returns:
            Dict with keys ``events`` (list) and ``summary``.
        """
        from mcp_audit import memory_circuit_breaker_status as _cb

        raw = _cb(limit=limit, since_ts=since_ts)
        if isinstance(raw, str):
            return json.loads(raw)
        return dict(raw)
