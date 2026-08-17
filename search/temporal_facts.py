"""Structured temporal and state ledger queries over kg_facts.

Provides deterministic O(1) relational queries for state counts,
point-in-time facts, temporal intervals, and recency ledgers.
"""

from __future__ import annotations

import logging
import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection

logger = logging.getLogger(__name__)


def query_state_count_from_ledger(
    db: AnyConnection,
    target_entity: str,
) -> dict[str, Any] | None:
    """Query the distinct historical values of an entity from kg_facts.

    Returns a dict with `count`, `values` (set of strings), and `source_memory_ids`,
    or None if no matching facts are found in the ledger.
    """
    if not target_entity:
        return None

    safe_target = target_entity.strip().lower()
    try:
        rows = db.execute(
            """
            SELECT object, source_memory, valid_at, event_time
            FROM kg_facts
            WHERE (LOWER(subject) = ? OR LOWER(predicate) = ? OR subject LIKE ? OR predicate LIKE ?)
              AND (invalidation_reason IS NULL OR invalidation_reason != 'error')
            ORDER BY COALESCE(event_time, valid_at, rowid) ASC
            """,
            (
                safe_target,
                safe_target,
                f"%{safe_target}%",
                f"%{safe_target}%",
            ),
        ).fetchall()

        if not rows:
            return None

        values = set()
        source_memories = []
        for r in rows:
            obj = (r[0] or "").strip()
            src = r[1] or ""
            if obj and len(obj) > 1:
                values.add(obj.lower())
            if src and src not in source_memories:
                source_memories.append(src)

        if not values:
            return None

        return {
            "entity": target_entity,
            "count": len(values),
            "values": sorted(values),
            "source_memories": source_memories,
        }
    except Exception as exc:
        logger.debug("query_state_count_from_ledger error for %s: %s", target_entity, exc)
        return None


def query_latest_fact_from_ledger(
    db: AnyConnection,
    target_entity: str,
) -> dict[str, Any] | None:
    """Query the most recent valid fact for an entity from kg_facts.

    Returns a dict with `subject`, `predicate`, `object`, `source_memory`,
    `event_time`, or None if unpopulated.
    """
    if not target_entity:
        return None

    safe_target = target_entity.strip().lower()
    try:
        rows = db.execute(
            """
            SELECT subject, predicate, object, source_memory, COALESCE(event_time, valid_at) as ts
            FROM kg_facts
            WHERE (LOWER(subject) = ? OR LOWER(predicate) = ? OR subject LIKE ? OR predicate LIKE ?)
              AND (invalidation_reason IS NULL OR invalidation_reason != 'error')
            ORDER BY COALESCE(event_time, valid_at, rowid) DESC
            LIMIT 1
            """,
            (
                safe_target,
                safe_target,
                f"%{safe_target}%",
                f"%{safe_target}%",
            ),
        ).fetchall()

        if not rows:
            return None

        row = rows[0]
        return {
            "subject": row[0],
            "predicate": row[1],
            "object": row[2],
            "source_memory": row[3],
            "timestamp": row[4],
        }
    except Exception as exc:
        logger.debug("query_latest_fact_from_ledger error for %s: %s", target_entity, exc)
        return None
