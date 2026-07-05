from __future__ import annotations
"""
CTR feedback and concept drift MCP tools — memory_record_ctr_feedback,
memory_check_concept_drift, memory_list_drift_alarms.

G4 fix (2026-06-22): documentation about the relationship between
``memory_record_ctr_feedback`` and ``memory_reinforce`` is in the
docstring of ``memory_record_ctr_feedback`` below.
"""
from mcp_common import _bootstrap_path  # noqa: E402,F401

import sqlite3
import time


import json
from typing import Optional

from mcp_common import (
    _resolve_memory_dir,
    _err,
    ErrorCode,
    with_audit,
)
from mcp_instance import mcp


@mcp.tool()
@with_audit("memory_record_ctr_feedback")
def memory_record_ctr_feedback(
    id: str,
    query_id: str,
    action: str = "returned",
    returned_at: Optional[float] = None,
    source: Optional[str] = None,
    ranking_params: Optional[str] = None,
) -> str:
    """Record click-through rate feedback for a search result.

    G4 fix (2026-06-22): ``memory_record_ctr_feedback`` (this tool)
    and ``memory_reinforce`` (mcp_memory.py) record two different
    signals on purpose — they are not interchangeable.

    * ``memory_record_ctr_feedback`` records the **implicit**
      signal: "the user *saw* this result in the response."  Writes
      a row to ``ctr_feedback`` with action=returned/clicked/etc.
      The search re-ranker reads this table to adjust ranking over
      time.  Use this when a search result is **delivered** to the
      user, regardless of whether the user does anything with it.

    * ``memory_reinforce`` records the **explicit** signal: "the
      user *judged* this memory useful (or not)."  Updates
      ``success_score`` and recomputes ``fitness_score``.  Use this
      when a user *acts on* a memory — e.g. cites it in a lesson,
      marks a decision as right, or undoes a save because the
      memory was wrong.  Skipping ``memory_reinforce`` on every
      "user saw it" event would over-credit the success score.

    In short: ``record_ctr_feedback`` = "delivered to user",
    ``reinforce`` = "user acted on it positively".  Call both when
    a user follows up on a search hit.  Call only
    ``record_ctr_feedback`` when the user just sees the result.
    Call only ``reinforce`` when the success/failure signal comes
    from outside the search path (e.g. a downstream agent confirms
    the memory was correct).
    """
    target_base = _resolve_memory_dir()
    db_path = target_base / "memory.db"
    if not db_path.exists():
        return _err(ErrorCode.DB_ERROR, f"No memory.db at {db_path}")
    try:
        from search.orchestrator import record_ctr_feedback_db

        record_ctr_feedback_db(
            db_path,
            id=id,
            query_id=query_id,
            action=action,
            returned_at=returned_at,
            source=source,
            ranking_params=ranking_params,
        )
        return json.dumps({"status": "ok", "action": action, "id": id})
    except Exception as e:
        return _err(ErrorCode.CTR_FEEDBACK_ERROR, str(e))


@mcp.tool()
@with_audit("memory_check_concept_drift")
def memory_check_concept_drift(threshold: float = 0.15) -> str:
    """Check for concept drift in the embedding space.

    When drift >= threshold, writes a row to concept_drift (centroid
    stream) and up to 10 per-memory rows to drift_alarms (the v15
    table). Use ``memory_list_drift_alarms`` to see the per-memory
    alarms, and pass ``acknowledged=True`` to clear them.
    """
    target_base = _resolve_memory_dir()
    db_path = target_base / "memory.db"
    if not db_path.exists():
        return _err(ErrorCode.DB_ERROR, f"No memory.db at {db_path}")
    try:
        from search_pipeline import check_concept_drift_db

        result = check_concept_drift_db(db_path, threshold=threshold)
        return json.dumps(result)
    except Exception as e:
        return _err(ErrorCode.CONCEPT_DRIFT_ERROR, str(e))


@mcp.tool()
@with_audit("memory_list_drift_alarms")
def memory_list_drift_alarms(
    acknowledged: Optional[bool] = None,
    alarm_level: Optional[str] = None,
    limit: int = 50,
    acknowledge_ids: Optional[list] = None,
    acknowledged_by: str = "operator",
    notes: str = "",
) -> str:
    """List and acknowledge per-memory concept-drift alarms.

    The ``drift_alarms`` table (added in v15) records one row per
    memory each time ``memory_check_concept_drift`` detects drift.
    Each alarm has a severity tier (info/warning/critical), a drift
    score snapshot, and an acknowledgement workflow.

    Args:
        acknowledged: If True, return only acknowledged alarms; if
            False, only unacknowledged; if None, return all. Default
            None.
        alarm_level: Filter by severity ('info' | 'warning' | 'critical').
            Default: no filter.
        limit: Max alarms to return (default 50, max 500).
        acknowledge_ids: If non-empty, mark these alarm IDs as
            acknowledged (with the given ``acknowledged_by`` and
            ``notes``) and return the updated list. Atomic: the
            ack-and-list happens in a single transaction.
        acknowledged_by: Operator/system name for acknowledgement
            audit trail. Default 'operator'.
        notes: Free-form notes added to the acknowledgement.

    Returns:
        JSON with keys: ``alarms`` (list of alarm dicts), ``count``,
        ``total_unacknowledged`` (for dashboard "needs attention"
        count), and ``acknowledged_now`` (count of alarms acked in
        this call, when applicable).
    """
    target_base = _resolve_memory_dir()
    db_path = target_base / "memory.db"
    if not db_path.exists():
        return _err(ErrorCode.DB_ERROR, f"No memory.db at {db_path}")
    if limit < 1 or limit > 500:
        return _err(ErrorCode.INVALID_PARAMS, "limit must be in [1, 500]")
    if alarm_level and alarm_level not in ("info", "warning", "critical"):
        return _err(
            ErrorCode.INVALID_PARAMS,
            "alarm_level must be one of info|warning|critical",
        )

    try:
        from infra.db import open_db
        with open_db(db_path, timeout=30.0, pooled=True, write=True) as conn:
            # 1. Acknowledge first, if requested (atomic with the read).
            acked_now = 0
            if acknowledge_ids:
                now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                placeholders = ",".join("?" * len(acknowledge_ids))
                params = [now_iso, acknowledged_by, notes] + list(acknowledge_ids)
                cur = conn.execute(
                    f"UPDATE drift_alarms "
                    f"SET acknowledged_at = ?, acknowledged_by = ?, notes = ? "
                    f"WHERE id IN ({placeholders}) AND acknowledged_at IS NULL",
                    params,
                )
                acked_now = cur.rowcount or 0
                conn.commit()

            # 2. Build the SELECT.
            where = []
            params = []
            if alarm_level:
                where.append("alarm_level = ?")
                params.append(alarm_level)
            if acknowledged is True:
                where.append("acknowledged_at IS NOT NULL")
            elif acknowledged is False:
                where.append("acknowledged_at IS NULL")
            query = "SELECT id, memory_id, concept, drift_score, threshold, alarm_level, detected_at, acknowledged_at, acknowledged_by, notes FROM drift_alarms"
            if where:
                query += " WHERE " + " AND ".join(where)
            query += " ORDER BY detected_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()

            total_unack_row = conn.execute(
                "SELECT COUNT(*) FROM drift_alarms WHERE acknowledged_at IS NULL"
            ).fetchone()
            total_unack = total_unack_row[0] if total_unack_row is not None else 0

            alarms = [
                {
                    "id": r[0],
                    "memory_id": r[1],
                    "concept": r[2],
                    "drift_score": r[3],
                    "threshold": r[4],
                    "alarm_level": r[5],
                    "detected_at": r[6],
                    "acknowledged_at": r[7],
                    "acknowledged_by": r[8],
                    "notes": r[9],
                }
                for r in rows
            ]
            return json.dumps(
                {
                    "alarms": alarms,
                    "count": len(alarms),
                    "total_unacknowledged": total_unack,
                    "acknowledged_now": acked_now,
                }
            )
    except sqlite3.OperationalError as e:
        # drift_alarms doesn't exist (pre-v15 DB). The migration
        # should have created it; surface the error so the operator
        # knows to run migration_runner.
        if "no such table: drift_alarms" in str(e):
            return _err(
                ErrorCode.DB_ERROR,
                "drift_alarms table missing — run migration_runner to apply v15",
            )
        return _err(ErrorCode.DB_ERROR, str(e))
    except Exception as e:
        return _err(ErrorCode.DB_ERROR, str(e))
