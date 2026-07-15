#!/usr/bin/env python3
"""Temporal Knowledge Graph Contradiction Resolution — DEPRECATED.

.. deprecated::
    This module is superseded by :mod:`fact.fact_temporal` for write-path
    temporal resolution (fact supersession via ``reconcile_fact_supersession``).
    The ``resolve_temporal_contradiction`` function here operates at the
    memory-note level, while ``fact_temporal`` operates at the KG-fact level
    with proper bi-temporal validity and entailment-chain propagation.

    Retained for backward compatibility and tests.  Will be removed in a
    future release.

Uses the existing ``valid_from`` / ``valid_to`` / ``superseded_by``
columns (already on the ``memories`` table) to automatically resolve
contradictions via temporal scoping.

How it works:
1. When a new fact conflicts with an existing one, the existing fact
   gets ``valid_to = now`` (it was true until now).
2. The new fact gets ``valid_from = now`` (it became true now).
3. The existing fact is marked with ``superseded_by = <new_note_id>``.
4. Search queries see both facts but can filter by time: "what did we
   know on date X?"

This turns contradictions from a problem into structured temporal data.
Instead of deleting old knowledge, we timestamp it and keep it searchable.

Also propagates contradictions through the knowledge graph:
If note A contradicts note B, and both share entity E, all facts
involving E are re-evaluated.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Optional
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Temporal contradiction resolution
# ---------------------------------------------------------------------------


def resolve_temporal_contradiction(
    db_path: str | Path,
    new_content: str,
    new_note_id: str,
    entity_name: Optional[str] = None,
    min_confidence: str = "low",
) -> dict:
    """Resolve contradictions between a new note and existing temporal facts.

    When a contradiction is detected (same entity, opposite polarity):
    1. The OLD fact gets ``valid_to = now`` (closed in time).
    2. The NEW fact gets ``valid_from = now`` (current from now).
    3. The OLD fact is marked with ``superseded_by = new_note_id``.

    Args:
        db_path: Path to the SQLite memory database.
        new_content: The text content of the new note.
        new_note_id: The note ID of the new note.
        entity_name: If provided, only check notes mentioning this entity.
        min_confidence: Minimum contradiction confidence ("low", "medium", "high").

    Returns:
        Dict with:
        - ``resolved``: int of contradictions resolved.
        - ``closed_notes``: list of note IDs that were closed (set valid_to).
        - ``superseded``: list of note IDs marked as superseded.
        - ``entity_propagation``: dict of entity → count of resolved edges.
    """

    from config import resolve_db_path
    from kg.contradiction_detector import detect_contradictions
    from infra._lazy_imports import open_db

    resolved_path = resolve_db_path(db_path)
    contradictions = detect_contradictions(
        memory_dir=str(resolved_path.parent),
        min_confidence=min_confidence,
    )

    now_iso = (
        __import__("datetime")
        .datetime.now(__import__("datetime").timezone.utc)
        .isoformat(timespec="seconds")
    )

    resolved = 0
    closed_notes: list[str] = []
    superseded: list[str] = []
    entity_propagation: dict[str, int] = {}

    target_notes = set()
    for c in contradictions:
        source = c.get("source", "")
        target = c.get("target", "")
        if source == new_note_id or target == new_note_id:
            target_notes.add(source if target == new_note_id else target)

    if not target_notes:
        return {
            "resolved": 0,
            "closed_notes": [],
            "superseded": [],
            "entity_propagation": {},
        }

    # Phase 1: Apply temporal resolution to each contradictory note
    with open_db(db_path, timeout=10.0) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        for note_id in target_notes:
            try:
                row = conn.execute(
                    "SELECT valid_to, superseded_by FROM memories WHERE id=?",
                    (note_id,),
                ).fetchone()
                if row and row[0] is None and row[1] is None:
                    # Note is currently "valid" — close its time window
                    conn.execute(
                        "UPDATE memories SET valid_to=?, superseded_by=? WHERE id=?",
                        (now_iso, new_note_id, note_id),
                    )
                    closed_notes.append(note_id)
                    superseded.append(note_id)
                    resolved += 1
                    logger.info(
                        "temporal_resolver: closed %s (superseded by %s)",
                        note_id,
                        new_note_id,
                    )
            except sqlite3.OperationalError as e:
                logger.warning("temporal_resolver: skip %s: %s", note_id, e)

        # Phase 1b: Invalidate KG edges for closed notes by extracting
        # entities from the closed note and invalidating edges between them.
        if closed_notes:
            try:
                # Check if kg_edges table exists
                kg_check = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kg_edges'"
                ).fetchone()
                if kg_check:
                    _all_entity_ids: set[int] = set()
                    for _cn in closed_notes:
                        _row = conn.execute(
                            "SELECT content FROM memories WHERE id=?", (_cn,)
                        ).fetchone()
                        if _row:
                            _entities = re.findall(
                                r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)\b", _row[0]
                            )
                            for _match in _entities:
                                if len(_match) > 3:
                                    _eid = conn.execute(
                                        "SELECT id FROM kg_entities WHERE name = ?",
                                        (_match.lower(),),
                                    ).fetchone()
                                    if _eid:
                                        _all_entity_ids.add(_eid[0])
                    if _all_entity_ids:
                        _ph = ",".join("?" * len(_all_entity_ids))
                        _eid_list = list(_all_entity_ids)
                        conn.execute(
                            f"UPDATE kg_edges SET invalid_at = ? "
                            f"WHERE invalid_at IS NULL "
                            f"AND (source_id IN ({_ph}) AND target_id IN ({_ph}))",
                            [now_iso] + _eid_list + _eid_list,
                        )
                        logger.info(
                            "temporal_resolver: invalidated KG edges for %d entities",
                            len(_eid_list),
                        )
            except sqlite3.OperationalError:
                pass  # kg_edges table may not exist

        # Phase 2: Graph propagation — traverse entities mentioned in the
        # closed notes and find other notes that share those entities.
        if entity_name:
            entities_to_check = [entity_name]
        else:
            # Extract entities from the closed notes' source files
            entities_to_check = []
            patterns = re.findall(r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)\b", new_content)
            for match in patterns:
                if len(match) > 3:
                    entities_to_check.append(match)

        for entity in entities_to_check[:10]:  # cap at 10 entities
            try:
                # Find notes that mention this entity
                related = conn.execute(
                    "SELECT id FROM memories WHERE content LIKE ? AND deleted_at IS NULL",
                    (f"%{entity}%",),
                ).fetchall()
                related_ids = [r[0] for r in related if r[0] != new_note_id]
                if related_ids:
                    conn.executemany(
                        "UPDATE memories SET valid_to=? WHERE id=? AND valid_to IS NULL",
                        [(now_iso, rid) for rid in related_ids],
                    )
                    entity_propagation[entity] = len(related_ids)
                    resolved += len(related_ids)
                    logger.info(
                        "temporal_resolver: propagated '%s' to %d related notes",
                        entity,
                        len(related_ids),
                    )
            except sqlite3.OperationalError:
                pass

        conn.commit()

    return {
        "resolved": len(target_notes) + sum(entity_propagation.values()),
        "closed_notes": closed_notes,
        "superseded": superseded,
        "entity_propagation": entity_propagation,
    }


def get_temporal_facts(
    db_path: str | Path,
    as_of: Optional[str] = None,
    note_id: Optional[str] = None,
) -> list[dict]:
    """Query temporal facts as of a specific timestamp.

    Useful for "what did we know on date X?" queries.

    Args:
        db_path: Path to the SQLite memory database.
        as_of: ISO timestamp. ``None`` = current time.
        note_id: If provided, only return facts for this note.

    Returns:
        List of dicts with: id, content, valid_from, valid_to, superseded_by.
    """
    from infra._lazy_imports import open_db

    db_path = Path(db_path)
    as_of = as_of or __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).isoformat(timespec="seconds")

    clauses = ["valid_from <= ?"]
    params: list = [as_of]
    if note_id:
        clauses.append("id = ?")
        params.append(note_id)

    with open_db(db_path, timeout=5.0) as conn:
        rows = conn.execute(
            f"SELECT id, content, valid_from, valid_to, superseded_by "
            f"FROM memories WHERE {' AND '.join(clauses)} "
            f"AND deleted_at IS NULL",
            params,
        ).fetchall()

        results = []
        for r in rows:
            results.append(
                {
                    "id": r[0],
                    "content": r[1][:200],
                    "valid_from": r[2],
                    "valid_to": r[3],
                    "superseded_by": r[4],
                    "status": "active" if r[3] is None else "superseded",
                }
            )
        return results
