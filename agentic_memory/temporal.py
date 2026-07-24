"""Temporal KG SDK — wraps MCP temporal tools as typed Python methods."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, cast

from agentic_memory.exceptions import (
    NotFoundError,
)
from agentic_memory.models import Fact
from agentic_memory.utils import (
    resolve_db_path,
)
from infra.db_write_queue import sqlite_write_queue


class TemporalKG:
    """Temporal KG operations wrapping the MCP temporal tools.

    Provides typed access to fact-level temporal knowledge graph
    features: time-aware queries, contradiction listing, supersession
    chain walking, and manual invalidation.

    Examples::

        tk = TemporalKG()
        facts = tk.search("user prefers dark mode")
        events = tk.contradictions()
        now_facts = tk.query_facts_at_time(time.time())
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = resolve_db_path(db_path)

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _parse_fact(d: dict[str, Any]) -> Fact:
        """Convert a raw dict from the KG into a Fact dataclass."""
        return Fact(
            id=str(d.get("id", "")),
            subject=d.get("subject", ""),
            predicate=d.get("predicate", ""),
            obj=d.get("object", d.get("obj", "")),
            confidence=float(d.get("confidence", 1.0)),
            source_memory=d.get("source_note_id", d.get("source_memory", "")),
            event_time=str(d.get("event_time", "") or ""),
            event_time_granularity=str(d.get("event_time_granularity", "") or ""),
            valid_at=str(d.get("valid_at", "") or ""),
            invalid_at=str(d.get("invalid_at", "") or ""),
            superseded_by=str(d.get("superseded_by", "") or ""),
            supersedes=str(d.get("supersedes", "") or ""),
            contradiction_score=float(d.get("contradiction_score", 0.0) or 0.0),
            locked=bool(d.get("locked", False)),
        )

    @staticmethod
    def _parse_json(raw: str | Any) -> Any:
        """Safely parse a JSON string, returning dicts/lists as-is."""
        if isinstance(raw, (dict, list)):
            return raw
        try:
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError, TypeError):
            return {}

    @staticmethod
    def _extract_rows(payload: Any) -> list[dict[str, Any]]:
        """Extract the ``rows`` list from a temporal query response."""
        data = TemporalKG._parse_json(payload)
        if isinstance(data, dict):
            rows = data.get("rows")
            if rows is None:
                rows = data.get("data", [])
            if rows is None:
                return []
            return cast(list[dict[str, Any]], rows)
        if isinstance(data, list):
            return data
        return []

    # ── Public API ──────────────────────────────────────────────────

    def search(self, query: str, limit: int = 10) -> list[Fact]:
        """Search facts with temporal awareness.

        Returns facts that are currently valid AND match the given
        text query (case-insensitive substring against
        subject/predicate/object).

        Args:
            query: Case-insensitive substring to match against
                subject, predicate, or object.
            limit: Max results to return (1..1000).  Default: 10.

        Returns:
            List of :class:`Fact` dataclasses that match the query
            and are valid at the current time.
        """
        from mcp_surface.mcp_audit import memory_temporal_query

        raw = memory_temporal_query(
            operation="at_time",
            as_of=time.time(),
            query=query,
            limit=limit,
        )
        rows = self._extract_rows(raw)
        return [self._parse_fact(r) for r in rows]

    def contradictions(
        self,
        since_ts: float | None = None,
        until_ts: float | None = None,
        reason: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List fact supersession/contradiction events.

        Each event describes one fact being replaced (superseded) by
        another, via the temporal KG auto-detector or manual override.

        Args:
            since_ts: Only events with transaction_time >= since_ts.
            until_ts: Only events with transaction_time <= until_ts.
            reason: Filter by invalidation reason (e.g. ``'contradicted'``).
            limit: Max rows to return (1..500).  Default: 50.
            offset: Skip first N rows (for paging).  Default: 0.

        Returns:
            List of dicts, each with keys ``old``, ``new``, ``reason``,
            ``contradiction_score``, and ``transaction_time``.
        """
        from mcp_surface.mcp_audit import memory_temporal_contradictions

        raw = memory_temporal_contradictions(
            since_ts=since_ts,
            until_ts=until_ts,
            reason=reason,
            limit=limit,
            offset=offset,
        )
        return self._extract_rows(raw)

    def query_facts_at_time(
        self,
        timestamp: float,
        query: str | None = None,
        limit: int = 50,
    ) -> list[Fact]:
        """Facts valid at the given epoch timestamp.

        A fact is "valid at *timestamp*" iff its ``valid_at`` is NULL
        or <= timestamp AND its ``invalid_at`` is NULL or >= timestamp.

        Args:
            timestamp: Epoch seconds (the time to query).
            query: Optional case-insensitive substring filter against
                subject/predicate/object.
            limit: Max results to return (1..1000).  Default: 50.

        Returns:
            List of :class:`Fact` dataclasses valid at *timestamp*.
        """
        from mcp_surface.mcp_audit import memory_temporal_query

        raw = memory_temporal_query(
            operation="at_time",
            as_of=timestamp,
            query=query,
            limit=limit,
        )
        rows = self._extract_rows(raw)
        return [self._parse_fact(r) for r in rows]

    def query_changed_since(
        self,
        timestamp: float,
        limit: int = 100,
    ) -> list[Fact]:
        """Facts that changed (inserted or invalidated) since *timestamp*.

        Args:
            timestamp: Epoch seconds lower bound.
            limit: Max results to return.  Default: 100.

        Returns:
            List of :class:`Fact` dataclasses changed since *timestamp*,
            ordered by most-recent change first.
        """
        from mcp_surface.mcp_audit import memory_temporal_query

        raw = memory_temporal_query(
            operation="changed_since",
            since_ts=timestamp,
            limit=limit,
        )
        rows = self._extract_rows(raw)
        return [self._parse_fact(r) for r in rows]

    def query_supersession_chain(
        self,
        fact_id: str | int,
    ) -> list[Fact]:
        """Walk the supersession chain for a fact (oldest first).

        Returns the full history of a fact across its replacements:
        ``[original, ..., latest]``.  The starting *fact_id* is
        included even if it has no ``superseded_by``.

        Args:
            fact_id: The fact ID to walk from (any link in the chain).

        Returns:
            List of :class:`Fact` dataclasses in chronological order.
            Empty if *fact_id* does not exist.
        """
        from mcp_surface.mcp_audit import memory_temporal_query

        raw = memory_temporal_query(
            operation="chain",
            fact_id=int(fact_id),
        )
        rows = self._extract_rows(raw)
        return [self._parse_fact(r) for r in rows]

    def invalidate_fact(
        self,
        fact_id: str | int,
        reason: str = "manual",
    ) -> bool:
        """Manually invalidate a fact (mark as no-longer-valid).

        Sets ``invalid_at`` and ``invalidation_reason`` on the fact.
        Does NOT set ``superseded_by`` — use this for facts removed
        from a memory or explicitly expired, not for contradictions.

        Args:
            fact_id: The fact ID to invalidate.
            reason: Optional reason string (default: ``'manual'``).

        Returns:
            True if the fact was updated.  False if the fact doesn't
            exist, is locked, or is already invalidated.

        Raises:
            NotFoundError: If the fact does not exist.
        """
        from fact.fact_temporal import invalidate_fact as _invalidate_fact

        conn = sqlite_write_queue.start_session(Path(self._db_path))
        try:
            ok = _invalidate_fact(conn, int(fact_id), reason=reason)
            if not ok:
                fact = conn.execute(
                    "SELECT id, locked FROM kg_facts WHERE id = ?",
                    (int(fact_id),),
                ).fetchone()
                if fact is None:
                    raise NotFoundError(f"Fact {fact_id} does not exist.")
            conn.commit()
            return ok
        finally:
            conn.close()
