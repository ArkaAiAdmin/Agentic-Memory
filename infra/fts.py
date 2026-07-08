"""FTS5-related helpers.

Extracted from memory_common.py during the 6-module refactor.

Provides:
  * ``cleanup_fts5_orphans(conn)``: remove FTS5 entries for soft-deleted/missing rows.
  * ``_migrate_fts5_porter_tokenizer(conn)`` (internal migration helper).
  * ``_migrate_ensure_fts_triggers(conn)`` (internal migration helper).

The two migration helpers are also re-exported by memory_common so existing
callers can still import them via ``from memory_common import _migrate_fts5_...``.
"""

from __future__ import annotations

import logging

import sqlite3
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection

logger = logging.getLogger(__name__)

__all__ = ["cleanup_fts5_orphans"]


def cleanup_fts5_orphans(conn: AnyConnection) -> int:
    """Remove FTS5 entries for soft-deleted or missing memories.

    Soft-delete uses UPDATE, which fires the memories_au trigger — the
    trigger DELETEs the old FTS5 row and INSERTs a new one with the same
    content.  This means soft-deleted notes remain in FTS5 indefinitely.
    Search queries already filter them out (AND deleted_at IS NULL), but
    they inflate FTS5 counts and slow down queries.

    This function removes FTS5 entries whose rowid points to a row with
    deleted_at IS NULL violated (i.e. the note is soft-deleted) or whose
    rowid has no corresponding memories row at all (orphaned).

    Returns the number of orphaned entries removed.
    """
    try:
        orphaned = conn.execute(
            "\n            SELECT fts.rowid FROM memories_fts fts\n"
            "            LEFT JOIN memories m ON m.rowid = fts.rowid\n"
            "            WHERE m.id IS NULL OR m.deleted_at IS NOT NULL\n"
            "        "
        ).fetchall()
        if not orphaned:
            return 0
        conn.executemany(
            "DELETE FROM memories_fts WHERE rowid = ?",
            [(rowid,) for (rowid,) in orphaned],
        )
        conn.commit()
        return len(orphaned)
    except Exception as exc:
        logger.warning("FTS5 orphan cleanup failed: %s", exc)
        return 0


def _create_fts5_table(conn: AnyConnection) -> None:
    """Create memories_fts virtual table with porter unicode61 and sync triggers."""
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5("
        "id, content, tags, category, tokenize='porter unicode61')"
    )
    try:
        conn.execute(
            "INSERT INTO memories_fts(rowid, id, content, tags, category) "
            "SELECT rowid, id, content, tags, category FROM memories WHERE deleted_at IS NULL"
        )
    except sqlite3.OperationalError:
        conn.execute("DELETE FROM memories_fts")
    conn.execute("DROP TRIGGER IF EXISTS memories_ai")
    conn.execute("DROP TRIGGER IF EXISTS memories_ad")
    conn.execute("DROP TRIGGER IF EXISTS memories_au")
    conn.execute(
        "CREATE TRIGGER memories_ai AFTER INSERT ON memories "
        "WHEN new.deleted_at IS NULL BEGIN "
        "INSERT INTO memories_fts(rowid, id, content, tags, category) "
        "VALUES (new.rowid, new.id, new.content, new.tags, new.category); END"
    )
    conn.execute(
        "CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN "
        "DELETE FROM memories_fts WHERE rowid = old.rowid; END"
    )
    conn.execute(
        "CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN "
        "DELETE FROM memories_fts WHERE rowid = old.rowid; "
        "INSERT INTO memories_fts(rowid, id, content, tags, category) "
        "SELECT new.rowid, new.id, new.content, new.tags, new.category WHERE new.deleted_at IS NULL; END"
    )


def _migrate_fts5_porter_tokenizer(conn: AnyConnection) -> None:
    """Create or upgrade memories_fts with porter unicode61 tokenizer.

    If the table doesn't exist, creates it from scratch with proper
    tokenizer and sync triggers. If it exists with unicode61 (old),
    rebuilds with porter stemming. If it already has porter, no-op.

    Porter stemming dramatically improves recall for inflected queries
    ("running" matches "run", "cats" matches "cat").

    Retries up to 3 times on failure to handle a known SQLite FTS5 race
    where concurrent ``CREATE VIRTUAL TABLE IF NOT EXISTS`` calls on
    different connections can both pass the existence check before
    the vtable constructor completes (issue observed in concurrent
    test fixtures).
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
    ).fetchone()
    if not row:
        _create_fts5_table(conn)
        return
    create_sql = row[0] or ""
    if "porter" in create_sql.lower():
        return
    for attempt in range(3):
        try:
            conn.execute("SAVEPOINT fts5_porter_sp")
            try:
                conn.execute("DROP TABLE IF EXISTS memories_fts")
                _create_fts5_table(conn)
                conn.execute("RELEASE SAVEPOINT fts5_porter_sp")
            except Exception as e:
                logger.warning("_migrate_fts5_porter_tokenizer failed: %s", e)
                conn.execute("ROLLBACK TO SAVEPOINT fts5_porter_sp")
                conn.execute("RELEASE SAVEPOINT fts5_porter_sp")
                raise
            break
        except Exception as e:
            logger.warning("_migrate_fts5_porter_tokenizer failed: %s", e)
            if attempt == 2:
                return
            time.sleep(0.05 * (attempt + 1))


def _migrate_ensure_fts_triggers(conn: AnyConnection) -> None:
    """Recreate the FTS5 sync triggers if missing.

    rebuild_index.py defines these inline; legacy DBs may have a
    memories_fts virtual table without triggers. We only create them
    if absent and the memories_fts table exists.
    """
    fts_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories_fts'"
    ).fetchone()
    if not fts_exists:
        return
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    if "memories_ai" not in existing:
        conn.execute(
            "\n            CREATE TRIGGER memories_ai AFTER INSERT ON memories\n"
            "            WHEN new.deleted_at IS NULL\n"
            "            BEGIN\n"
            "              INSERT INTO memories_fts(rowid, id, content, tags, category)\n"
            "              VALUES (new.rowid, new.id, new.content, new.tags, new.category);\n"
            "            END\n"
            "            "
        )
    if "memories_ad" not in existing:
        conn.execute(
            "\n            CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN\n"
            "              DELETE FROM memories_fts WHERE rowid = old.rowid;\n"
            "            END\n"
            "            "
        )
    if "memories_au" not in existing:
        conn.execute(
            "\n            CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN\n"
            "              DELETE FROM memories_fts WHERE rowid = old.rowid;\n"
            "              INSERT INTO memories_fts(rowid, id, content, tags, category)\n"
            "              SELECT new.rowid, new.id, new.content, new.tags, new.category WHERE new.deleted_at IS NULL;\n"
            "            END\n"
            "            "
        )
