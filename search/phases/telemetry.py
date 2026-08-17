"""Phase 14 Telemetry functions."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Optional

from infra.error_counter import increment as _phase_inc

if TYPE_CHECKING:
    from infra.db import AnyConnection

logger = logging.getLogger(__name__)

# Backward-compatible phase latency tracking (pre-error_counter API).
_phase_latencies: dict[str, float] = {}
_phase_latencies_lock = threading.Lock()


def _record_phase_latency(name: str, start_time: float) -> None:
    """Record elapsed wall-clock latency for *name* into _phase_latencies."""
    elapsed_ms = (time.time() - start_time) * 1000.0
    with _phase_latencies_lock:
        _phase_latencies[name] = elapsed_ms


def _record_search_telemetry(
    *,
    db: AnyConnection | None,
    query_id: str,
    result_items: list,
    ctr_weights: Optional[dict],
    query_type: Optional[str] = None,
) -> None:
    """Record CTR feedback and adaptive-retention access events for the result set.

    Two side-effects, both best-effort:

    1. Write a row to ``memory_ctr_feedback`` so the next CTR computation
       can correlate this query's result set with user-click behavior.
    2. Write 'impression' rows to ``memory_search_interaction`` for all
       results so CTR statistics can be aggregated by query type.
    3. Call ``adaptive_retention.record_access`` for each result row so
       the per-note fitness decay respects the fresh access.

    All exceptions are swallowed — telemetry is informational, not a
    precondition for the user seeing results.
    """
    if db is None:
        return
    try:
        _now = time.time()
        _ranking = json.dumps({"weights": ctr_weights}) if ctr_weights else "{}"
        # One impression row per returned result, keyed by (query_id, id),
        # so a later click/dismiss (record_ctr_feedback_db) can stamp the
        # same row and compute_channel_weights learns the real signal.
        # ON CONFLICT preserves clicked_at/dismissed_at across re-impressions.
        # M25 fix: batch all CTR feedback inserts with executemany().
        _ctr_params = []
        for _r in result_items:
            _rid = _r.get("id") if isinstance(_r, dict) else None
            if not _rid:
                continue
            _ctr_params.append((query_id, _rid, _now, _ranking))
        if _ctr_params:
            db.executemany(
                "INSERT INTO memory_ctr_feedback "
                "(query_id, id, returned_at, source, ranking_params) "
                "VALUES (?, ?, ?, 'search', ?) "
                "ON CONFLICT(query_id, id) DO UPDATE SET "
                "returned_at=excluded.returned_at, "
                "ranking_params=excluded.ranking_params",
                _ctr_params,
            )
        db.commit()
    except Exception as e:
        _phase_inc("search.telemetry.ctr_feedback", e)
        if isinstance(e, sqlite3.OperationalError) and ("locked" in str(e).lower() or "busy" in str(e).lower()):
            logger.debug("record_search_telemetry CTR skipped (database busy/locked): %s", e)
        else:
            logger.warning("record_search_telemetry CTR failed: %s", e)
    try:
        try:
            _tenant_row = db.execute("SELECT tenant_id()").fetchone()
            tenant_id = _tenant_row[0] if _tenant_row else "default"
        except Exception:
            tenant_id = "default"
        # M25 fix: batch search_interaction inserts with executemany().
        _interaction_params = []
        for i, r in enumerate(result_items):
            _interaction_params.append(
                (query_id, r.get("id"), tenant_id, i + 1, query_type or "general")
            )
        if _interaction_params:
            db.executemany(
                "INSERT INTO memory_search_interaction "
                "(query_id, memory_id, action, tenant_id, rank, query_type) "
                "VALUES (?, ?, 'impression', ?, ?, ?) "
                "ON CONFLICT(query_id, memory_id, action) "
                "DO UPDATE SET ts=excluded.ts, rank=excluded.rank, query_type=coalesce(excluded.query_type, memory_search_interaction.query_type)",
                _interaction_params,
            )
        db.commit()
    except Exception as e:
        _phase_inc("search.telemetry.search_interaction", e)
        if isinstance(e, sqlite3.OperationalError) and ("locked" in str(e).lower() or "busy" in str(e).lower()):
            logger.debug("record_search_telemetry search_interaction skipped (database busy/locked): %s", e)
        else:
            logger.warning("record_search_telemetry search_interaction failed: %s", e)
    try:
        from adaptive_retention import record_access

        for r in result_items:
            note_id = r.get("id", "")
            if note_id:
                record_access(db, note_id, "search")
        db.commit()
    except Exception as e:
        _phase_inc("search.telemetry.adaptive_retention", e)
        if isinstance(e, sqlite3.OperationalError) and ("locked" in str(e).lower() or "busy" in str(e).lower()):
            logger.debug("record_search_telemetry adaptive_retention skipped (database busy/locked): %s", e)
        else:
            logger.warning("record_search_telemetry adaptive_retention failed: %s", e)


def _record_search_phase_latencies(*, db, query_id: str, phase_latencies: dict[str, float]) -> None:
    """Persist per-phase latency to the search_phase_stats table.

    Best-effort: never propagates. Writes one row per phase with the
    latency in milliseconds and a UTC ISO timestamp for aggregation.
    """
    try:
        if not phase_latencies:
            return
        now_ts = time.time()
        rows = [
            (query_id, name, latency_ms, now_ts)
            for name, latency_ms in phase_latencies.items()
        ]
        db.executemany(
            "INSERT INTO search_phase_stats (query_id, phase_name, latency_ms, created_at) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        db.commit()
    except Exception as e:
        if isinstance(e, sqlite3.OperationalError) and ("locked" in str(e).lower() or "busy" in str(e).lower()):
            logger.debug("_record_search_phase_latencies skipped (database busy/locked): %s", e)
        else:
            logger.warning("_record_search_phase_latencies failed: %s", e)
