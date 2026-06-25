"""Shared cleanup for memory-related rows.

B-3 fix (2026-06-22 follow-up): the saga rollback path and the
hard_delete_note() flow both need to clean up the rows that depend on a
``memories`` row (kg_edges, kg_entities that became unreferenced, and
backlinks).  Previously the cleanup logic was duplicated in
``memory_delete.py``; this module centralises it so the two callers
can't drift apart.

Why centralise?
    * ``memory_delete.hard_delete_note`` already calls three helpers
      (``_cascading_delete_relations``, ``_remove_from_indices``,
      ``_purge_orphaned_kg``) for the same job.  The saga rollback
      path didn't have an equivalent, which was the audit gap.
    * The cleanup has subtle ordering: orphaned ``kg_edges`` first
      (they reference ``kg_entities``), then orphaned ``kg_entities``
      (which are only considered orphaned once no edges and no facts
      reference them).  Easy to get wrong when duplicated.
    * Future operations (e.g. a soft-delete cascade, or a
      bulk-archive job) will need the same cleanup.  Centralising now
      keeps the rule "clean up related rows" in one place.

Public surface:
    * :func:`cleanup_memory_relations` — remove kg_edges, kg_entities,
      and backlinks rows tied to a given ``note_id`` (or that became
      orphaned as a result).

The functions are best-effort: each is wrapped in try/except so a
schema mismatch on a legacy DB logs and continues, not raises.  This
matches the convention in ``memory_delete._purge_orphaned_kg``.
"""

from __future__ import annotations

__all__ = [
    "cleanup_memory_relations",
    "remove_kg_relations_for_note",
    "remove_backlinks_for_note",
    "remove_chunks_and_embeddings_for_note",
]

import logging
import sqlite3

logger = logging.getLogger(__name__)


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Return True if *table_name* exists in the database.

    Cheap PRAGMA lookup; used to gate cleanup calls on older databases
    that may not have all tables.  Mirrors the helper in
    ``memory_delete.py`` (extracted 2026-06-22).
    """
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        is not None
    )


def remove_backlinks_for_note(conn: sqlite3.Connection, note_id: str) -> int:
    """Delete backlinks rows where source_id (or its slug) equals *note_id*.

    By design, target_id may refer to non-existent notes (wiki-style
    "red links"), so we only delete when the source side matches.  We
    also match on the slug-only form because forward-references in
    some legacy code stored just the slug, not the full
    ``category/title`` form.

    Returns the number of rows actually deleted.  Logs and swallows
    on OperationalError so a schema mismatch is non-fatal.
    """
    if not _table_exists(conn, "backlinks"):
        return 0
    slug = note_id.split("/", 1)[1] if "/" in note_id else note_id
    try:
        cur = conn.execute(
            "DELETE FROM backlinks WHERE source_id IN (?, ?)",
            (note_id, slug),
        )
    except sqlite3.OperationalError as exc:
        logger.warning("remove_backlinks_for_note(%s): %r", note_id, exc)
        return 0
    deleted = cur.rowcount if cur.rowcount is not None else 0
    return int(deleted)


def remove_kg_relations_for_note(
    conn: sqlite3.Connection, note_id: str
) -> dict[str, int]:
    """Remove kg_edges / kg_entities / kg_facts rows tied to *note_id*.

    A note's contribution to the KG comes in two shapes:

    1. ``kg_facts`` rows where ``source_memory = note_id`` — these are
       facts *extracted from* this note.  Deleting them is unambiguous.

    2. ``kg_edges`` rows don't reference notes directly — they connect
       entities.  So we can't delete a specific edge on behalf of a
       note; instead we delete edges that reference entities whose
       only remaining fact is from this note.  This is the "orphaned
       entity" cleanup.

    ``kg_entities`` rows are *not* deleted here even if they become
    unreferenced — they may be referenced by other notes' facts.  The
    caller should run ``memory_integrity.repair_kg_orphans`` separately
    to clean up entities that are no longer referenced by any note.

    Returns a dict with per-table counts.
    """
    counts = {"kg_facts": 0, "kg_edges": 0}
    if not _table_exists(conn, "kg_facts"):
        return counts
    try:
        cur = conn.execute("DELETE FROM kg_facts WHERE source_memory = ?", (note_id,))
        counts["kg_facts"] = int(cur.rowcount or 0)
    except sqlite3.OperationalError as exc:
        logger.warning("remove_kg_relations_for_note(%s) kg_facts: %r", note_id, exc)

    if not (_table_exists(conn, "kg_edges") and _table_exists(conn, "kg_entities")):
        return counts
    # Orphan edges: those whose source_id or target_id is an entity
    # that is no longer referenced by any kg_facts.
    try:
        cur = conn.execute(
            """
            DELETE FROM kg_edges WHERE source_id IN (
                SELECT e.id FROM kg_entities e
                WHERE NOT EXISTS (
                    SELECT 1 FROM kg_facts f
                    WHERE f.subject = e.name OR f.object = e.name
                )
            ) OR target_id IN (
                SELECT e.id FROM kg_entities e
                WHERE NOT EXISTS (
                    SELECT 1 FROM kg_facts f
                    WHERE f.subject = e.name OR f.object = e.name
                )
            )
            """
        )
        counts["kg_edges"] = int(cur.rowcount or 0)
    except sqlite3.OperationalError as exc:
        logger.warning("remove_kg_relations_for_note(%s) kg_edges: %r", note_id, exc)
    return counts


def cleanup_memory_relations(conn: sqlite3.Connection, note_id: str) -> dict[str, int]:
    """Remove all dependent rows for *note_id* in one call.

    Convenience wrapper used by:

    * ``memory_delete.hard_delete_note`` (replaces the
      ``_cascading_delete_relations`` + ``_remove_from_indices`` +
      ``_purge_orphaned_kg`` three-step sequence).
    * ``save.saga.undo_upsert`` (new in 2026-06-22 follow-up — the
      audit gap).

    The order is:

    1. ``kg_facts`` rows where ``source_memory = note_id``.
    2. Orphan ``kg_edges`` (edges that referenced entities whose only
       remaining fact was from this note).
    3. ``backlinks`` rows where ``source_id`` matches.

    We do NOT delete ``kg_entities`` here — entities are shared across
    notes, and deleting them based on a single note's relationship is
    too aggressive.  Use ``memory_integrity.repair_kg_orphans`` to
    remove entities that are truly unreferenced by any note.

    We do NOT delete ``kg_entities`` here — entities are shared across
    notes, and deleting them based on a single note's relationship is
    too aggressive.  Use ``memory_integrity.repair_kg_orphans`` to
    remove entities that are truly unreferenced by any note.

    We do NOT delete ``memory_chunks``, ``memory_embeddings``,
    ``memory_vec_keys`` here — they are only orphaned during an
    UPDATE-style saga rollback (where the row is restored, not deleted),
    so the caller (``saga._undo_upsert``) handles them separately via
    :func:`remove_chunks_and_embeddings_for_note`.

    Returns a dict with per-table counts for observability.
    """
    kg = remove_kg_relations_for_note(conn, note_id)
    backlinks = remove_backlinks_for_note(conn, note_id)
    return {
        "kg_facts": kg["kg_facts"],
        "kg_edges": kg["kg_edges"],
        "backlinks": backlinks,
    }


def remove_chunks_and_embeddings_for_note(
    conn: sqlite3.Connection, note_id: str
) -> dict[str, int]:
    """Remove ``memory_chunks`` and ``memory_embeddings`` for *note_id*.

    Needed for UPDATE-style saga rollback: the ``memories`` row is
    restored (not deleted), so the ON DELETE CASCADE FK does not fire.
    Without explicit cleanup, chunks and embeddings written between the
    upsert and the rollback become orphans.

    Best-effort: each table is wrapped in try/except so a schema
    mismatch on a legacy DB logs and continues.
    """
    counts: dict[str, int] = {}
    try:
        cur = conn.execute("DELETE FROM memory_chunks WHERE parent_id = ?", (note_id,))
        counts["memory_chunks"] = int(cur.rowcount or 0)
    except Exception as exc:
        logger.warning("remove_chunks(%s): %r", note_id, exc)
    try:
        cur = conn.execute(
            "DELETE FROM memory_embeddings WHERE memory_id = ?", (note_id,)
        )
        counts["memory_embeddings"] = int(cur.rowcount or 0)
    except Exception as exc:
        logger.warning("remove_embeddings(%s): %r", note_id, exc)
    try:
        cur = conn.execute(
            "DELETE FROM memory_vec_keys WHERE memory_id = ?", (note_id,)
        )
        counts["memory_vec_keys"] = int(cur.rowcount or 0)
    except Exception as exc:
        logger.warning("remove_vec_keys(%s): %r", note_id, exc)
    return counts
