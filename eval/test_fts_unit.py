#!/usr/bin/env python3
"""Unit tests for fts.py.

The module has one public function (``cleanup_fts5_orphans``) and
two internal migration helpers (``_create_fts5_table``,
``_migrate_fts5_porter_tokenizer``,
``_migrate_ensure_fts_triggers``).

Strategy: spin up a minimal in-memory SQLite with just the
``memories`` + ``memories_fts`` tables we need, run the public
function, assert on counts.

Why not the prod schema? FTS5 needs the full schema (triggers,
audit_log, etc.) for any meaningful integration test; that's
covered by ``test_no_silent_search_failures.py`` already. This
file isolates the fts.py contract.
"""

import sqlite3
import sys
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))
sys.path.insert(0, str(INSTALL_DIR / "eval"))


def _create_minimal_db() -> sqlite3.Connection:
    """Build an in-memory DB with a memories table and a porter
    unicode61 FTS5 virtual table + sync triggers. Mirrors the
    production schema shape (sans the other 24 tables).

    Note: we use a regular (non-external-content) FTS5 table here
    because cleanup_fts5_orphans' LEFT JOIN against memories only
    needs the FTS rowids to match memory rowids — it doesn't read
    the FTS content. External-content FTS5 makes the unit test
    brittle without changing the contract under test."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE memories ("
        "  id TEXT PRIMARY KEY,"
        "  content TEXT,"
        "  tags TEXT,"
        "  category TEXT,"
        "  deleted_at TEXT"
        ")"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE memories_fts USING fts5("
        "id, content, tags, category, tokenize='porter unicode61')"
    )
    # Sync triggers.
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
        "SELECT new.rowid, new.id, new.content, new.tags, new.category "
        "WHERE new.deleted_at IS NULL; END"
    )
    return conn


class TestCleanupFts5Orphans(unittest.TestCase):
    def setUp(self):
        self.conn = _create_minimal_db()

    def tearDown(self):
        self.conn.close()

    def test_returns_zero_when_no_orphans(self):
        from fts import cleanup_fts5_orphans

        # Insert a non-deleted memory; the AI trigger writes the FTS row.
        self.conn.execute(
            "INSERT INTO memories(id, content, tags, category, deleted_at) "
            "VALUES ('a', 'hello world', '[]', 'test', NULL)"
        )
        self.conn.commit()
        self.assertEqual(cleanup_fts5_orphans(self.conn), 0)

    def test_removes_orphaned_fts_rows_with_no_memories_match(self):
        """An FTS row that points to a non-existent memory row is an
        orphan — the LEFT JOIN in cleanup_fts5_orphans must surface
        and remove it. We construct the orphan directly because
        constructing it via the trigger path is brittle in unit-test
        SQLite configurations."""
        from fts import cleanup_fts5_orphans

        # Manually insert a memory so we have a known live rowid.
        self.conn.execute(
            "INSERT INTO memories(id, content, tags, category) "
            "VALUES ('live', 'live content', '[]', 'test')"
        )
        self.conn.commit()
        # Insert an FTS row at a rowid that has no memory.
        self.conn.execute(
            "INSERT INTO memories_fts(rowid, id, content, tags, category) "
            "VALUES (999999, 'orphan', 'orphan content', '[]', 'test')"
        )
        self.conn.commit()
        # We now have 2 FTS rows: the live one (from the AI trigger)
        # and the orphan (manually inserted).
        before = self.conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
        self.assertEqual(before, 2)
        removed = cleanup_fts5_orphans(self.conn)
        self.assertEqual(removed, 1)
        after = self.conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
        self.assertEqual(after, 1)

    def test_returns_count_as_int(self):
        from fts import cleanup_fts5_orphans

        result = cleanup_fts5_orphans(self.conn)
        self.assertIsInstance(result, int)
        self.assertGreaterEqual(result, 0)


class TestCreateFts5Table(unittest.TestCase):
    """_create_fts5_table is internal but the public behavior is
    observable: after calling it, memories_fts must exist with
    porter unicode61 tokenizer and the three sync triggers must be
    present."""

    def test_creates_table_with_porter_tokenizer(self):
        from fts import _create_fts5_table

        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "CREATE TABLE memories ("
                "  id TEXT PRIMARY KEY, content TEXT, tags TEXT,"
                "  category TEXT, deleted_at TEXT)"
            )
            _create_fts5_table(conn)
            row = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='memories_fts'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIn("porter", row[0].lower())
        finally:
            conn.close()

    def test_creates_three_sync_triggers(self):
        from fts import _create_fts5_table

        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "CREATE TABLE memories ("
                "  id TEXT PRIMARY KEY, content TEXT, tags TEXT,"
                "  category TEXT, deleted_at TEXT)"
            )
            _create_fts5_table(conn)
            triggers = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                ).fetchall()
            }
            for required in ("memories_ai", "memories_ad", "memories_au"):
                self.assertIn(required, triggers)
        finally:
            conn.close()


class TestMigrateFts5PorterTokenizer(unittest.TestCase):
    def test_no_op_when_porter_already_present(self):
        """If the table is already porter, the migration must be a
        no-op (no DROP TABLE) — that's the documented fast path."""
        from fts import _create_fts5_table, _migrate_fts5_porter_tokenizer

        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "CREATE TABLE memories ("
                "  id TEXT PRIMARY KEY, content TEXT, tags TEXT,"
                "  category TEXT, deleted_at TEXT, rowid INTEGER)"
            )
            _create_fts5_table(conn)
            # Insert a sentinel row we can use to detect destruction.
            conn.execute(
                "INSERT INTO memories(id, content, tags, category) "
                "VALUES ('sentinel', 'keep me', '[]', 'test')"
            )
            conn.commit()
            _migrate_fts5_porter_tokenizer(conn)
            # The sentinel row must still exist.
            row = conn.execute("SELECT id FROM memories WHERE id='sentinel'").fetchone()
            self.assertEqual(row[0], "sentinel")
        finally:
            conn.close()


class TestMigrateEnsureFtsTriggers(unittest.TestCase):
    def test_creates_missing_triggers(self):
        from fts import _migrate_ensure_fts_triggers

        conn = sqlite3.connect(":memory:")
        try:
            # Set up the table without any triggers.
            conn.execute(
                "CREATE VIRTUAL TABLE memories_fts USING fts5("
                "id, content, tags, category, tokenize='porter unicode61')"
            )
            conn.execute(
                "CREATE TABLE memories ("
                "  id TEXT PRIMARY KEY, content TEXT, tags TEXT,"
                "  category TEXT, deleted_at TEXT)"
            )
            _migrate_ensure_fts_triggers(conn)
            triggers = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                ).fetchall()
            }
            for required in ("memories_ai", "memories_ad", "memories_au"):
                self.assertIn(required, triggers)
        finally:
            conn.close()

    def test_no_op_when_fts_table_missing(self):
        """If the FTS table doesn't exist, the function must not
        crash and must not create anything."""
        from fts import _migrate_ensure_fts_triggers

        conn = sqlite3.connect(":memory:")
        try:
            # Don't create memories_fts.
            _migrate_ensure_fts_triggers(conn)  # must not raise
            triggers = list(
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                ).fetchall()
            )
            self.assertEqual(triggers, [])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
