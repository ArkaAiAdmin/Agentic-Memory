"""CTR feedback and response interaction recording."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Map the legacy ``memory_record_ctr_feedback`` action vocabulary onto the
# unified ``memory_search_interaction`` action vocabulary.  Each legacy action
# becomes its OWN row (keyed by (query_id, memory_id, action)), so a
# returned+clicked pair for the same query/memory no longer collapses into a
# single row the way ``INSERT OR REPLACE`` on ``memory_ctr_feedback`` did.
_CTR_FEEDBACK_ACTION_MAP = {
    "returned": "impression",
    "clicked": "click",
    "dismissed": "dismissed",
}


def record_ctr_feedback_db(
    db_path: str | Path,
    memory_id: str,
    query_id: str,
    action: str = "returned",
    returned_at: Optional[float] = None,
    source: Optional[str] = None,
    ranking_params: Optional[str] = None,
    tenant_id: str = "default",
) -> None:
    """Record CTR feedback as a ``memory_search_interaction`` row and correlate
    the click/dismiss signal into ``memory_ctr_feedback``.

    Phase 0 (audit #9) fix: previously this wrote to ``memory_ctr_feedback``
    with ``INSERT OR REPLACE``, which collapsed multi-event rows.  We now
    write one row per (query_id, memory_id, action) into
    ``memory_search_interaction`` using ``ON CONFLICT(query_id, memory_id,
    action) DO UPDATE`` so re-recording only refreshes ``ts``/``rank``.

    FIX 2 (search-pipeline review): the click/dismiss signal is also
    correlated back onto the per-(query_id, id) impression row that
    ``_record_search_telemetry`` wrote during the originating search, by
    stamping ``clicked_at`` / ``dismissed_at``.  ``compute_channel_weights``
    then reads the real CTR signal instead of always returning ``None``.
    This is best-effort — a missing impression row (e.g. a DB at the legacy
    single-PK schema) simply leaves the UPDATE as a no-op.

    Args:
        db_path: Path to the memory SQLite database.
        memory_id: Memory id; maps to the ``memory_ctr_feedback.id`` column and to
            ``memory_search_interaction.memory_id``.
        query_id: Search query correlation id.
        action: One of ``returned`` / ``clicked`` / ``dismissed`` (legacy) or
            any ``memory_search_interaction`` action string.
        returned_at: Optional explicit timestamp (epoch seconds).
        source: Legacy column, no longer stored (kept for signature compat).
        ranking_params: Legacy column, no longer stored (kept for compat).
        tenant_id: Tenant namespace for multi-tenant isolation.
    """
    from infra._lazy_imports import connection_pool, safe_close_db

    mapped_action = _CTR_FEEDBACK_ACTION_MAP.get(action, action)
    conn = connection_pool.get(str(db_path), tenant_id=tenant_id)
    try:
        query_type = None
        try:
            row = conn.execute(
                "SELECT query_type FROM memory_search_interaction "
                "WHERE query_id = ? AND query_type IS NOT NULL LIMIT 1",
                (query_id,),
            ).fetchone()
            if row:
                query_type = row[0]
        except Exception:
            pass
        conn.execute(
            "INSERT INTO memory_search_interaction "
            "(query_id, memory_id, action, tenant_id, rank, query_type) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(query_id, memory_id, action) "
            "DO UPDATE SET ts=excluded.ts, rank=excluded.rank, query_type=coalesce(excluded.query_type, memory_search_interaction.query_type)",
            (query_id, memory_id, mapped_action, tenant_id, None, query_type),
        )
        conn.commit()
        # Correlate the click/dismiss signal onto the matching impression row
        # so compute_channel_weights learns. Best-effort; never breaks the
        # interaction write above.
        if action in ("clicked", "dismissed"):
            try:
                _now = time.time()
                if action == "clicked":
                    conn.execute(
                        "UPDATE memory_ctr_feedback SET clicked_at=? "
                        "WHERE query_id=? AND id=?",
                        (_now, query_id, memory_id),
                    )
                else:
                    conn.execute(
                        "UPDATE memory_ctr_feedback SET dismissed_at=? "
                        "WHERE query_id=? AND id=?",
                        (_now, query_id, memory_id),
                    )
                conn.commit()
            except Exception as _ctr_exc:
                logger.debug("CTR feedback correlation skipped: %s", _ctr_exc)
    finally:
        safe_close_db(conn)


def record_memory_used_in_response(
    db_path: str | Path,
    query_id: str,
    memory_ids: list[str],
    tenant_id: str = "default",
    ranks: Optional[list[int]] = None,
    query_type: Optional[str] = None,
) -> None:
    """Record that the given memories were actually presented to the
    user/agent for ``query_id`` (the ``used_in_response`` CTR signal).

    One row per (query_id, memory_id) is written to
    ``memory_search_interaction`` with ``action='used_in_response'``.  Uses
    ``ON CONFLICT(query_id, memory_id, action) DO UPDATE`` so re-recording the
    same pair only bumps ``ts``/``rank`` (never collapses multi-event rows).

    This is the producer counterpart to ``record_ctr_feedback_db``: it fires
    when a recalled memory is surfaced in a response (e.g. the session-start
    recap injected into the system prompt), which is a stronger signal than a
    mere search impression.

    Args:
        db_path: Path to the memory SQLite database.
        query_id: Search query correlation id.
        memory_ids: Memory ids that were shown.
        tenant_id: Tenant namespace for multi-tenant isolation.
        ranks: Optional 1-based display ranks aligned to ``memory_ids``.
        query_type: Optional query type for grouping interactions.
    """
    if not memory_ids:
        return
    from infra._lazy_imports import connection_pool, safe_close_db

    conn = connection_pool.get(str(db_path), tenant_id=tenant_id)
    try:
        # If query_type is not provided, try to look it up from an existing impression row
        if not query_type:
            try:
                row = conn.execute(
                    "SELECT query_type FROM memory_search_interaction "
                    "WHERE query_id = ? AND query_type IS NOT NULL LIMIT 1",
                    (query_id,),
                ).fetchone()
                if row:
                    query_type = row[0]
            except Exception:
                pass
        for i, memory_id in enumerate(memory_ids):
            rank = ranks[i] if (ranks is not None and i < len(ranks)) else None
            conn.execute(
                "INSERT INTO memory_search_interaction "
                "(query_id, memory_id, action, tenant_id, rank, query_type) "
                "VALUES (?, ?, 'used_in_response', ?, ?, ?) "
                "ON CONFLICT(query_id, memory_id, action) "
                "DO UPDATE SET ts=excluded.ts, rank=excluded.rank, query_type=coalesce(excluded.query_type, memory_search_interaction.query_type)",
                (query_id, memory_id, tenant_id, rank, query_type),
            )
            # Implicit click signal: stamp clicked_at on CTR impressions
            # for memories used in the response.  This closes the feedback
            # loop — the search pipeline records impressions (Phase 14),
            # and when the agent cites a memory, we treat that as a click.
            try:
                conn.execute(
                    "UPDATE memory_ctr_feedback "
                    "SET clicked_at = COALESCE(clicked_at, ?) "
                    "WHERE query_id = ? AND id = ? AND clicked_at IS NULL",
                    (time.time(), query_id, memory_id),
                )
            except Exception:
                pass  # Best-effort; never breaks the primary write
        conn.commit()
    finally:
        safe_close_db(conn)
