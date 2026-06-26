#!/usr/bin/env python3
"""Soft-delete + restore + trash lifecycle for the agentic-memory system.

Background
----------
The previous memory model was "delete means hard-delete". This module
adds a 30-day restore window: notes move to a trash bucket on delete,
remain searchable *by explicit listing only* for 30 days, and are then
purged by ``purge_expired()``.

Design decisions
----------------

1. "30 days" = exactly 30 * 86400 = 2,592,000 seconds, measured from the
   note's ``deleted_at`` timestamp to ``datetime.now(timezone.utc)`` at query time.
   This is a fixed sliding window, NOT a calendar-month boundary. A note
   soft-deleted at ``2026-06-07T10:00:00Z`` becomes purgeable at
   ``>= 2026-07-07T10:00:00Z``. Rationale: same definition across all
   paths (purge_expired, list_trash, hard_delete_note) and trivially
   testable; calendar-month math would require timezone-aware date logic
   for negligible practical benefit at 30-day granularity.

2. ``days_until_purge`` is a float = (30 * 86400 - elapsed_seconds) / 86400.
   Negative when the note is already past the 30-day window. A value of
   0.0 means "due for purge right now".

3. ``ensure_deleted_at_column`` also ensures ``deleted_by TEXT`` exists,
   because ``soft_delete_note`` writes both columns in the same UPDATE.
   Renaming the helper would be a breaking change for callers, so the
   function name stays and the docstring documents the dual-add.

4. Hard-deleting an *active* note is only allowed if the note is older
   than 30 days (by ``created_at``). For notes <30 days old, you must
   soft-delete first; this is the safety net that prevents the trash
   workflow from being a footgun.

5. FTS5 is maintained by the existing ``memories_ai`` / ``memories_au``
   / ``memories_ad`` triggers in the prod DB. Soft-delete and restore
   are ``UPDATE`` statements -> the ``memories_au`` trigger does
   delete-then-insert on the FTS index, preserving the row. Hard-delete
   fires ``memories_ad`` which removes the FTS row. We never write to
   the FTS table directly.

6. Backlinks are removed in both directions (``source_id = ?`` OR
   ``target_id = ?``) on hard-delete. Soft-delete and restore leave
   backlinks alone — the link graph doesn't know about the trash
   lifecycle.

7. Concurrency: every function opens its own connection via
   ``open_db(timeout=30)`` (which sets ``busy_timeout = 30000ms``).
   Transactions are short — no network calls, no sleep, no held locks
   across statements. SQLite serialises writers, but with a 30s wait
   budget concurrent writers succeed rather than fail with
   ``SQLITE_BUSY``.

Public API
----------
- ``ensure_deleted_at_column(db_path)``
- ``soft_delete_note(db_path, note_id, deleted_by="user")``
- ``restore_note(db_path, note_id)``
- ``hard_delete_note(db_path, note_id)``
- ``list_trash(db_path, include_expired=False)``
- ``purge_expired(db_path)``
- ``is_soft_deleted(db_path, note_id)``
- ``delete_active_where(db_path, where_clause, params)``
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, List, Optional, Tuple, cast

from memory_common import open_db
from db import AnyConnection

__all__ = [
    "RESTORE_WINDOW_SECONDS",
    "ensure_deleted_at_column",
    "soft_delete_note",
    "restore_note",
    "hard_delete_note",
    "list_trash",
    "purge_expired",
    "is_soft_deleted",
    "delete_active_where",
]


logger = logging.getLogger(__name__)


RESTORE_WINDOW_SECONDS: int = 30 * 86400  # see design note 1
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9/._-]+$")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_note_id(note_id: Any) -> str:
    """Validate ``note_id`` is a non-empty string of safe characters.

    Allowed: ASCII alphanumerics, ``/``, ``.``, ``-``, ``_``. Any other
    character (whitespace, quotes, semicolons, dashes that look like SQL
    comments) is rejected *before* the value touches SQL.

    Raises ``ValueError`` on invalid input. The caller is expected to
    catch and return ``False`` (or whatever the function's contract is).
    """
    if not isinstance(note_id, str):
        raise ValueError(f"note_id must be str, got {type(note_id).__name__}")
    if not note_id:
        raise ValueError("note_id must be non-empty")
    if not _SAFE_ID_RE.match(note_id):
        raise ValueError(f"note_id contains unsafe characters: {note_id!r}")
    # Reject path-traversal patterns even though the char set allows '.'
    # and '/'. These never appear in legitimate canonical memory ids.
    if ".." in note_id:
        raise ValueError(f"note_id contains path-traversal sequence: {note_id!r}")
    if note_id.startswith("/"):
        raise ValueError(f"note_id must not be an absolute path: {note_id!r}")
    return note_id


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Centralised so tests can monkey-patch it and so every write uses the
    same format. UTC, not local — keeps the soft-delete window
    timezone-independent.
    """
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _now_dt() -> _dt.datetime:
    """Return the current UTC ``datetime`` for arithmetic."""
    return _dt.datetime.now(_dt.timezone.utc)


def _has_column(conn: AnyConnection, table: str, column: str) -> bool:
    """Return True if ``table`` has a column named ``column``.

    Uses ``PRAGMA table_info`` so it picks up columns added by ALTER
    TABLE. Case-sensitive on the column name (SQLite default).
    """
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def _has_trigger(conn: AnyConnection, trigger_name: str) -> bool:
    """Return True if a trigger named ``trigger_name`` exists."""
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
        (trigger_name,),
    )
    return cur.fetchone() is not None


def _ensure_columns(conn: AnyConnection) -> None:
    """Add ``deleted_at`` and ``deleted_by`` columns to ``memories`` if missing.

    Idempotent: re-running is a no-op. Both columns live next to the
    other lifecycle columns (``valid_to``, ``superseded_by``).
    """
    for col in ("deleted_at", "deleted_by"):
        if not _has_column(conn, "memories", col):
            logger.info("Adding column memories.%s", col)
            conn.execute(f"ALTER TABLE memories ADD COLUMN {col} TEXT")
    conn.commit()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ensure_deleted_at_column(db_path) -> None:
    """Idempotently add the ``deleted_at`` (+ ``deleted_by``) column.

    The function name is singular because that's how the requirement
    was originally written, but it ensures BOTH columns exist (see
    design note 3 in the module docstring).

    Args:
        db_path: Path to a SQLite memory DB. The ``memories`` table
            must exist (no-op if missing).
    """
    db_path = Path(db_path)
    if not db_path.exists():
        logger.warning("ensure_deleted_at_column: db %s does not exist", db_path)
        return
    try:
        with open_db(db_path) as conn:
            # If the memories table doesn't exist, do nothing — the
            # DB hasn't been bootstrapped yet and there is nothing to
            # migrate. The caller will create the table later.
            if not _has_column(conn, "sqlite_master", "name"):
                return
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
            )
            if cur.fetchone() is None:
                return
            _ensure_columns(conn)
    except Exception as e:
        logger.warning("ensure_deleted_at_column failed for %s: %s", db_path, e)


def _invalidate_edges_for_note(conn: AnyConnection, note_id: str) -> None:
    """Invalidate edges tied to entities that appear only in this note.

    Called by soft_delete_note to mark edges as stale when the note
    that produced them is soft-deleted.
    """
    try:
        # Find all entities that appear only in this note's facts
        entity_rows = conn.execute(
            """SELECT DISTINCT e.id
               FROM kg_entities e
               JOIN kg_facts f ON e.name = f.subject OR e.name = f.object
               WHERE f.source_memory = ?""",
            (note_id,),
        ).fetchall()
        if not entity_rows:
            return
        entity_ids = [r[0] for r in entity_rows]
        placeholders = ",".join("?" * len(entity_ids))
        # Find edges involving those entities
        edge_rows = conn.execute(
            f"""SELECT id FROM kg_edges
                WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})""",
            tuple(entity_ids + entity_ids),
        ).fetchall()
        for (edge_id,) in edge_rows:
            conn.execute(
                "UPDATE kg_edges SET invalid_at = datetime('now') WHERE id = ? AND invalid_at IS NULL",
                (edge_id,),
            )
    except sqlite3.OperationalError:
        # kg_edges or kg_entities may not exist yet — ignore
        pass


def _restore_edges_for_note(conn: AnyConnection, note_id: str) -> None:
    """Re-validate edges that were invalidated when this note was soft-deleted.

    Called by restore_note to clear the invalid_at flag when a note
    is restored from the trash.
    """
    try:
        # Find all entities that appear in this note's facts
        entity_rows = conn.execute(
            """SELECT DISTINCT e.id
               FROM kg_entities e
               JOIN kg_facts f ON e.name = f.subject OR e.name = f.object
               WHERE f.source_memory = ?""",
            (note_id,),
        ).fetchall()
        if not entity_rows:
            return
        entity_ids = [r[0] for r in entity_rows]
        placeholders = ",".join("?" * len(entity_ids))
        # Clear invalid_at for edges involving those entities
        conn.execute(
            f"""UPDATE kg_edges SET invalid_at = NULL
                WHERE (source_id IN ({placeholders}) OR target_id IN ({placeholders}))
                AND invalid_at IS NOT NULL""",
            tuple(entity_ids + entity_ids),
        )
    except sqlite3.OperationalError:
        # kg_edges or kg_entities may not exist yet — ignore
        pass


def soft_delete_note(
    db_path,
    note_id: str,
    deleted_by: str = "user",
) -> bool:
    """Mark ``note_id`` as soft-deleted.

    Sets ``deleted_at = datetime.now(timezone.utc).isoformat()`` and
    ``deleted_by = deleted_by``. The underlying row stays in place,
    backlinks stay in place, and the FTS5 entry stays in place (the
    ``memories_au`` trigger handles index maintenance automatically).

    Idempotent: calling on an already-soft-deleted note is a no-op
    that returns False. Returns False also for unknown ``note_id`` or
    any DB error. Invalid ``note_id`` is rejected with ``ValueError``
    so the caller can distinguish "bad input" from "DB error".

    Args:
        db_path: Path to the memory DB.
        note_id: Canonical memory id (validated, see design notes).
        deleted_by: Free-text label of who/what deleted it. Defaults
            to ``"user"``; cleanup scripts may pass e.g. ``"purge"``.

    Returns:
        True on a successful state change (note went from active to
        soft-deleted). False if the note does not exist or was already
        soft-deleted.
    """
    note_id = _validate_note_id(note_id)
    if not isinstance(deleted_by, str) or not deleted_by:
        raise ValueError("deleted_by must be a non-empty string")
    try:
        with open_db(db_path) as conn:
            # Check existence + current state in one round trip.
            row = conn.execute(
                "SELECT deleted_at FROM memories WHERE id = ?", (note_id,)
            ).fetchone()
            if row is None:
                return False
            if row[0] is not None:
                # Already soft-deleted — idempotent no-op.
                return False
            now = _now_iso()
            conn.execute(
                "UPDATE memories SET deleted_at = ?, deleted_by = ? WHERE id = ?",
                (now, deleted_by, note_id),
            )
            # ── Invalidate edges for entities tied to this note ──
            _invalidate_edges_for_note(conn, note_id)
            conn.commit()
            return True
    except Exception as e:
        logger.warning("soft_delete_note(%s) failed: %s", note_id, e)
        return False


def restore_note(db_path, note_id: str) -> bool:
    """Clear ``deleted_at`` and ``deleted_by`` on a soft-deleted note.

    Idempotent in the sense that restoring an already-active note is a
    no-op that returns False. Returns False also for unknown
    ``note_id`` or any DB error. Invalid ``note_id`` is rejected with
    ``ValueError``.

    The FTS5 index is maintained by the existing
    ``memories_au`` trigger; the content/tags didn't change so the
    index is identical before and after.

    Args:
        db_path: Path to the memory DB.
        note_id: Canonical memory id.

    Returns:
        True on a successful state change (note went from
        soft-deleted to active). False otherwise.
    """
    note_id = _validate_note_id(note_id)
    try:
        with open_db(db_path) as conn:
            row = conn.execute(
                "SELECT deleted_at FROM memories WHERE id = ?", (note_id,)
            ).fetchone()
            if row is None:
                return False
            if row[0] is None:
                return False
            conn.execute(
                "UPDATE memories SET deleted_at = NULL, deleted_by = NULL WHERE id = ?",
                (note_id,),
            )
            # ── Re-validate edges for entities tied to this note ──
            _restore_edges_for_note(conn, note_id)
            conn.commit()
            return True
    except Exception as e:
        logger.warning("restore_note(%s) failed: %s", note_id, e)
        return False


def _table_exists(conn: AnyConnection, table_name: str) -> bool:
    """Return True if the named table exists in the schema.

    Used by hard_delete_note() to gate cascading deletes on table
    presence. Extracted 2026-06-22 to deduplicate the
    ``SELECT 1 FROM sqlite_master`` checks.
    """
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _cascading_delete_relations(conn: AnyConnection, note_id: str) -> None:
    """Step 1 of the hard-delete cascade: backlinks.

    Delete by full note_id AND by slug (forward-references may store
    just the slug, not the full category/slug id). Extracted
    2026-06-22 from hard_delete_note().
    """
    slug = note_id.split("/", 1)[1] if "/" in note_id else note_id
    conn.execute(
        "DELETE FROM backlinks WHERE source_id = ? OR target_id = ? "
        "OR source_id = ? OR target_id = ?",
        (note_id, note_id, slug, slug),
    )


def _remove_from_indices(conn: AnyConnection, note_id: str) -> None:
    """Steps 2-5 of the hard-delete cascade: chunks, embeddings,
    vec_keys, kg_facts. Each is gated on the table existing (older
    databases may not have all of these). Extracted 2026-06-22.
    """
    if _table_exists(conn, "memory_chunks"):
        conn.execute("DELETE FROM memory_chunks WHERE parent_id = ?", (note_id,))
    if _table_exists(conn, "memory_embeddings"):
        conn.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (note_id,))
    if _table_exists(conn, "memory_vec_keys"):
        conn.execute("DELETE FROM memory_vec_keys WHERE memory_id = ?", (note_id,))
    if _table_exists(conn, "kg_facts"):
        conn.execute("DELETE FROM kg_facts WHERE source_memory = ?", (note_id,))


def _purge_orphaned_kg(conn: AnyConnection) -> None:
    """Steps 6-7 of the hard-delete cascade: remove KG edges and
    entities that became orphaned (no remaining facts or edges
    referencing them). Wrapped in try/except OperationalError so a
    schema mismatch on a legacy DB is logged, not fatal.

    Extracted 2026-06-22 from hard_delete_note().
    """
    if _table_exists(conn, "kg_edges") and _table_exists(conn, "kg_facts"):
        try:
            conn.execute("""
                DELETE FROM kg_edges WHERE source_id IN (
                    SELECT e.id FROM kg_entities e
                    WHERE NOT EXISTS (SELECT 1 FROM kg_facts f WHERE f.subject = e.name OR f.object = e.name)
                ) OR target_id IN (
                    SELECT e.id FROM kg_entities e
                    WHERE NOT EXISTS (SELECT 1 FROM kg_facts f WHERE f.subject = e.name OR f.object = e.name)
                )
            """)
        except sqlite3.OperationalError as e:
            logger.warning("kg_edges cleanup in hard_delete_note: %s", e)

    if (
        _table_exists(conn, "kg_entities")
        and _table_exists(conn, "kg_facts")
        and _table_exists(conn, "kg_edges")
    ):
        try:
            conn.execute("""
                DELETE FROM kg_entities WHERE id IN (
                    SELECT e.id FROM kg_entities e
                    WHERE NOT EXISTS (SELECT 1 FROM kg_edges e2 WHERE e2.source_id = e.id OR e2.target_id = e.id)
                    AND NOT EXISTS (SELECT 1 FROM kg_facts f WHERE f.subject = e.name OR f.object = e.name)
                )
            """)
        except sqlite3.OperationalError as e:
            logger.warning("kg_entities cleanup in hard_delete_note: %s", e)


def _purge_fts5_if_no_trigger(conn: AnyConnection, note_id: str) -> None:
    """Step 8: if the ``memories_ad`` trigger doesn't exist (legacy
    / partially-migrated DB), delete the FTS5 row manually. If the
    trigger exists, the FTS5 cleanup happens automatically when
    ``memories`` is deleted in step 9.

    Extracted 2026-06-22 from hard_delete_note().
    """
    if _has_trigger(conn, "memories_ad"):
        return
    try:
        conn.execute(
            "DELETE FROM memories_fts WHERE rowid = ("
            "  SELECT m.rowid FROM memories m WHERE m.id = ?"
            ")",
            (note_id,),
        )
    except Exception as exc:
        logger.warning("manual FTS5 delete in hard_delete_note(%s): %s", note_id, exc)


def _purge_markdown_file(note_id: str) -> None:
    """P0-4 fix: also remove the on-disk .md file. Best-effort — log
    and continue if it fails. Extracted 2026-06-22.
    """
    try:
        from mcp_common import get_memory_paths

        paths = get_memory_paths()  # (project_root, local_mem, global_mem)
        memory_dir = paths[1] if len(paths) > 1 else paths[0] / "memory"
        if memory_dir and "/" in note_id:
            cat, slug = note_id.split("/", 1)
            md_path = Path(memory_dir) / cat / f"{slug}.md"
            if md_path.exists():
                md_path.unlink()
    except Exception as md_exc:
        logger.warning("hard_delete_note(%s): .md cleanup failed: %s", note_id, md_exc)


def _check_active_age(conn: AnyConnection, note_id: str, row) -> None:
    """If the note is active (not soft-deleted), enforce the 30-day
    safety net. Raises ValueError if too young. Extracted 2026-06-22.
    """
    created_at, deleted_at = row[0], row[1]
    if deleted_at is not None:
        return
    try:
        # P0-8 fix: fromisoformat preserves any existing timezone. If
        # the column was stored with a non-UTC offset (e.g. +05:00),
        # the previous .replace(tzinfo=utc) silently kept the wall-clock
        # time but changed the offset. Normalize via astimezone(utc).
        created_dt = _dt.datetime.fromisoformat(created_at)
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=_dt.timezone.utc)
        else:
            created_dt = created_dt.astimezone(_dt.timezone.utc)
    except (TypeError, ValueError):
        logger.warning(
            "hard_delete_note(%s): unparseable created_at=%r", note_id, created_at
        )
        raise _SkipDelete("unparseable created_at")
    age_seconds = (_now_dt() - created_dt).total_seconds()
    if age_seconds < RESTORE_WINDOW_SECONDS:
        raise ValueError(
            f"refusing to hard-delete active note {note_id!r} "
            f"that is only {age_seconds / 86400:.1f} days old "
            f"(must be >30 days or soft-deleted first)"
        )


class _SkipDelete(Exception):
    """Internal control-flow signal: row existed but was skipped.

    Used by _check_active_age() to signal "unparseable created_at"
    back to hard_delete_note() without leaking the implementation
    detail.
    """

    pass


def hard_delete_note(db_path, note_id: str) -> bool:
    """Permanently remove a note and ALL associated data from the DB.

    Cascading removals:
        1. ``memories`` row (fires ``memories_ad`` trigger → FTS5 removal).
        2. ``backlinks`` where source_id or target_id matches.
        3. ``memory_chunks`` where parent_id matches (triggers clean FTS5).
        4. ``memory_embeddings`` where memory_id matches.
        5. ``memory_vec_keys`` where memory_id matches.
        6. ``kg_facts`` where source_memory matches.
        7. ``kg_edges`` referencing KG entities that become orphaned.
        8. ``kg_entities`` that become orphaned (no remaining edges/facts).

    Validity rules:
        * The note must exist (False otherwise).
        * If the note is currently active, it must be older than
          30 days (by ``created_at``). This is the safety net — fresh
          active notes cannot be hard-deleted directly. Active notes
          past 30 days can be removed (e.g. for GDPR-style cleanup).
        * Already-soft-deleted notes are always hard-deletable,
          regardless of age.

    Raises:
        ValueError: if the note exists, is active, and is younger
            than 30 days. This distinguishes "you forgot to
            soft-delete first" from "DB error" so callers can react
            appropriately.

    Args:
        db_path: Path to the memory DB.
        note_id: Canonical memory id.

    Returns:
        True on successful delete. False if the note does not exist
        or any DB error.

    Decomposed 2026-06-22: the 9-step cascade is now a sequence of
    named helpers. The orchestrator below reads as a 1-step-per-line
    list of the cascade.
    """
    note_id = _validate_note_id(note_id)
    try:
        with open_db(db_path) as conn:
            row = conn.execute(
                "SELECT created_at, deleted_at FROM memories WHERE id = ?",
                (note_id,),
            ).fetchone()
            if row is None:
                return False
            try:
                _check_active_age(conn, note_id, row)
            except _SkipDelete:
                return False

            _cascading_delete_relations(conn, note_id)
            _remove_from_indices(conn, note_id)
            # B-3 fix (2026-06-22 follow-up): delegate the orphan
            # cleanup to the shared helper so the saga rollback path
            # has the same behaviour.  The helper covers kg_facts,
            # orphan kg_edges, and backlinks — the same set this
            # function used to handle inline via _purge_orphaned_kg +
            # the duplicate _cascading_delete_relations.
            from save.cleanup import cleanup_memory_relations

            cleanup_memory_relations(cast(sqlite3.Connection, conn), note_id)
            _purge_fts5_if_no_trigger(conn, note_id)
            conn.execute("DELETE FROM memories WHERE id = ?", (note_id,))
            conn.commit()
            _purge_markdown_file(note_id)
            return True
    except ValueError:
        raise
    except Exception as e:
        logger.warning("hard_delete_note(%s) failed: %s", note_id, e)
        return False


def list_trash(db_path, include_expired: bool = False) -> List[dict]:
    """List all soft-deleted notes, oldest first.

    Each entry is a dict with:
        - ``id`` (str)
        - ``source_file`` (str)
        - ``deleted_at`` (str, ISO-8601 UTC)
        - ``deleted_by`` (str)
        - ``days_until_purge`` (float; negative if past the 30-day window)

    Args:
        db_path: Path to the memory DB.
        include_expired: If False (default), omit notes whose
            ``deleted_at`` is older than 30 days — those are
            ``purge_expired``'s problem, not yours. If True, return
            the full trash including the expired tail.

    Returns:
        A list of dicts, oldest first. Empty list on any DB error.
    """
    try:
        with open_db(db_path, row_factory=sqlite3.Row) as conn:
            cur = conn.execute(
                """
                SELECT id, source_file, deleted_at, deleted_by
                  FROM memories
                 WHERE deleted_at IS NOT NULL
                 ORDER BY deleted_at ASC
                """
            )
            now = _now_dt()
            results: List[dict] = []
            for row in cur.fetchall():
                deleted_at_str = row["deleted_at"]
                try:
                    deleted_dt = _dt.datetime.fromisoformat(deleted_at_str).replace(
                        tzinfo=_dt.timezone.utc
                    )
                except (TypeError, ValueError):
                    # Corrupt timestamp — treat as expired to avoid
                    # getting stuck.
                    days_until_purge = -1.0
                    elapsed = RESTORE_WINDOW_SECONDS + 1
                else:
                    elapsed = int((now - deleted_dt).total_seconds())
                    days_until_purge = (RESTORE_WINDOW_SECONDS - elapsed) / 86400.0
                if not include_expired and elapsed >= RESTORE_WINDOW_SECONDS:
                    continue
                results.append(
                    {
                        "id": row["id"],
                        "source_file": row["source_file"],
                        "deleted_at": deleted_at_str,
                        "deleted_by": row["deleted_by"],
                        "days_until_purge": days_until_purge,
                    }
                )
            return results
    except Exception as e:
        logger.warning("list_trash failed: %s", e)
        return []


def purge_expired(db_path) -> int:
    """Hard-delete all soft-deleted notes whose 30-day window has elapsed.

    Full cascade removal per note:
        1. ``memories`` row (fires ``memories_ad`` trigger → FTS5 removal).
        2. ``backlinks`` where source_id or target_id matches.
        3. ``memory_chunks`` where parent_id matches (triggers clean FTS5).
        4. ``memory_embeddings`` where memory_id matches.
        5. ``memory_vec_keys`` where memory_id matches.
        6. ``kg_facts`` where source_memory matches.
        7. ``kg_edges`` referencing KG entities that become orphaned.
        8. ``kg_entities`` that become orphaned (no remaining edges/facts).

    Args:
        db_path: Path to the memory DB.

    Returns:
        Count of notes purged. 0 if none are expired or on any DB
        error.
    """
    try:
        with open_db(db_path) as conn:
            cutoff = _now_dt() - _dt.timedelta(seconds=RESTORE_WINDOW_SECONDS)
            cutoff_iso = cutoff.isoformat()
            cur = conn.execute(
                "SELECT id FROM memories WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                (cutoff_iso,),
            )
            expired_ids = [r[0] for r in cur.fetchall()]
            if not expired_ids:
                return 0
            placeholders = ",".join("?" for _ in expired_ids)

            # ── 1. Backlinks ───────────────────────────────────
            conn.execute(
                f"DELETE FROM backlinks WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
                tuple(expired_ids + expired_ids),
            )

            # ── 2. Chunks (memory_chunks_ad trigger cleans FTS5) ─
            if (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    ("memory_chunks",),
                ).fetchone()
                is not None
            ):
                conn.execute(
                    f"DELETE FROM memory_chunks WHERE parent_id IN ({placeholders})",
                    tuple(expired_ids),
                )

            # ── 3. Embeddings ──────────────────────────────────
            if (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    ("memory_embeddings",),
                ).fetchone()
                is not None
            ):
                conn.execute(
                    f"DELETE FROM memory_embeddings WHERE memory_id IN ({placeholders})",
                    tuple(expired_ids),
                )

            # ── 4. Vec index keys ──────────────────────────────
            if (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    ("memory_vec_keys",),
                ).fetchone()
                is not None
            ):
                conn.execute(
                    f"DELETE FROM memory_vec_keys WHERE memory_id IN ({placeholders})",
                    tuple(expired_ids),
                )

            # ── 5. KG facts ────────────────────────────────────
            if (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    ("kg_facts",),
                ).fetchone()
                is not None
            ):
                conn.execute(
                    f"DELETE FROM kg_facts WHERE source_memory IN ({placeholders})",
                    tuple(expired_ids),
                )

            # ── 6. KG edges referencing orphaned entities ───────
            if (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    ("kg_edges",),
                ).fetchone()
                is not None
                and conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    ("kg_facts",),
                ).fetchone()
                is not None
            ):
                try:
                    conn.execute("""
                        DELETE FROM kg_edges WHERE source_id IN (
                            SELECT e.id FROM kg_entities e
                            WHERE NOT EXISTS (SELECT 1 FROM kg_facts f WHERE f.subject = e.name OR f.object = e.name)
                        ) OR target_id IN (
                            SELECT e.id FROM kg_entities e
                            WHERE NOT EXISTS (SELECT 1 FROM kg_facts f WHERE f.subject = e.name OR f.object = e.name)
                        )
                    """)
                except sqlite3.OperationalError as e:
                    logger.warning("step 6 (kg_edges) in purge_expired: %s", e)

            # ── 7. Orphaned KG entities ────────────────────────
            if (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    ("kg_entities",),
                ).fetchone()
                is not None
                and conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    ("kg_facts",),
                ).fetchone()
                is not None
                and conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    ("kg_edges",),
                ).fetchone()
                is not None
            ):
                try:
                    conn.execute("""
                        DELETE FROM kg_entities WHERE id IN (
                            SELECT e.id FROM kg_entities e
                            WHERE NOT EXISTS (SELECT 1 FROM kg_edges e2 WHERE e2.source_id = e.id OR e2.target_id = e.id)
                            AND NOT EXISTS (SELECT 1 FROM kg_facts f WHERE f.subject = e.name OR f.object = e.name)
                        )
                    """)
                except sqlite3.OperationalError as e:
                    logger.warning("step 7 (kg_entities) in purge_expired: %s", e)

            # ── 8. FTS index ───────────────────────────────────
            trigger_exists = _has_trigger(conn, "memories_ad")

            # Delete FTS5 entries BEFORE deleting memories rows (need rowid lookup)
            if not trigger_exists:
                for nid in expired_ids:
                    try:
                        conn.execute(
                            "DELETE FROM memories_fts WHERE rowid = ("
                            "  SELECT m.rowid FROM memories m WHERE m.id = ?"
                            ")",
                            (nid,),
                        )
                    except Exception as exc:
                        logger.warning(
                            "manual FTS5 delete in purge_expired(%s): %s",
                            nid,
                            exc,
                        )

            # ── 9. Memories row ────────────────────────────────
            conn.execute(
                f"DELETE FROM memories WHERE id IN ({placeholders})",
                tuple(expired_ids),
            )

            conn.commit()
            return len(expired_ids)
    except Exception as e:
        logger.warning("purge_expired failed: %s", e)
        return 0


def is_soft_deleted(db_path, note_id: str) -> bool:
    """Return True if ``note_id`` exists and ``deleted_at IS NOT NULL``.

    Returns False for unknown ids. Returns False (not raise) on DB
    error. Invalid ``note_id`` is rejected with ``ValueError``.

    Args:
        db_path: Path to the memory DB.
        note_id: Canonical memory id.
    """
    note_id = _validate_note_id(note_id)
    try:
        with open_db(db_path) as conn:
            row = conn.execute(
                "SELECT deleted_at FROM memories WHERE id = ?", (note_id,)
            ).fetchone()
            if row is None:
                return False
            return row[0] is not None
    except Exception as e:
        logger.warning("is_soft_deleted(%s) failed: %s", note_id, e)
        return False


def delete_active_where(
    db_path,
    where_clause: str,
    params: Tuple[Any, ...] = (),
) -> int:
    """Bulk soft-delete active notes matching ``where_clause``.

    Intended for cleanup scripts. The ``where_clause`` is the SQL
    fragment after the ``WHERE`` keyword (the function also accepts a
    full clause starting with ``WHERE`` and strips the prefix). It is
    embedded into the query, so a caller who passes user input is
    doing it wrong — this is an internal-trust API.

    Safety: ``where_clause`` is rejected if it contains ``;`` (would
    allow statement stacking) or ``--`` (SQL comment). Parameter
    binding still applies to ``params``.

    Idempotent: re-running on already-soft-deleted rows is a no-op
    (the WHERE clause filters to active rows). Returns the number of
    rows that transitioned to soft-deleted state.

    Args:
        db_path: Path to the memory DB.
        where_clause: SQL fragment, e.g. ``"repo_id = ? AND pinned = 0"``
            or ``"WHERE repo_id = ?"``. Rejected if it contains ``;``
            or ``--``.
        params: Tuple of parameter values, bound via SQLite's
            parameter substitution.

    Returns:
        Count of notes soft-deleted. 0 on no match or DB error.
    """
    if not isinstance(where_clause, str) or not where_clause.strip():
        raise ValueError("where_clause must be a non-empty string")
    if ";" in where_clause:
        raise ValueError("where_clause must not contain ';' (multi-statement)")
    if "--" in where_clause:
        raise ValueError("where_clause must not contain SQL comment marker '--'")
    stripped = where_clause.strip()
    allowed = re.sub(r"[a-zA-Z0-9_., =<>!()?]", "", stripped)
    if allowed:
        logger.warning(
            "delete_active_where called with suspicious where_clause: %r", where_clause
        )
    clause = where_clause.strip()
    upper = clause.upper()
    if upper == "WHERE":
        raise ValueError("where_clause has no predicate (bare 'WHERE' keyword)")
    if upper.startswith("WHERE "):
        clause = clause[6:].strip()
    if not clause:
        raise ValueError("where_clause has no predicate after stripping WHERE")
    if params is None:
        params = ()
    try:
        with open_db(db_path) as conn:
            now = _now_iso()
            # Only touch active rows. RETURNING is SQLite 3.35+ but
            # we use a separate count to stay compatible.
            cur = conn.execute(
                f"""
                UPDATE memories
                   SET deleted_at = ?, deleted_by = 'bulk'
                 WHERE deleted_at IS NULL AND ({clause})
                """,
                (now,) + tuple(params),
            )
            changed = cur.rowcount if cur.rowcount is not None else 0
            # rowcount can be -1 if the driver doesn't track it; fall
            # back to a SELECT COUNT.
            if changed < 0:
                row = conn.execute(
                    f"""
                    SELECT COUNT(*) FROM memories
                     WHERE deleted_at = ? AND ({clause})
                    """,
                    (now,) + tuple(params),
                ).fetchone()
                if row is not None:
                    changed = row[0]
            # ── Invalidate edges for bulk-deleted notes ──────
            if changed > 0:
                deleted_ids = [
                    r[0]
                    for r in conn.execute(
                        f"""
                        SELECT id FROM memories
                         WHERE deleted_at = ? AND deleted_by = 'bulk'
                           AND ({clause})
                        """,
                        (now,) + tuple(params),
                    ).fetchall()
                ]
                for nid in deleted_ids:
                    _invalidate_edges_for_note(conn, nid)
            conn.commit()
            return int(changed)
    except ValueError:
        raise
    except Exception as e:
        logger.warning("delete_active_where failed: %s", e)
        return -1
