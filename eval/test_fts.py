"""Tests for FTS5 helpers in fts.py."""

import sqlite3
import unittest


class TestCleanupFts5Orphans(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT,
                content TEXT,
                tags TEXT,
                category TEXT,
                deleted_at TEXT
            );
            INSERT INTO memories (id, content, tags, category, deleted_at)
            VALUES ('note/1', 'active content', '', 'lessons', NULL);
            INSERT INTO memories (id, content, tags, category, deleted_at)
            VALUES ('note/2', 'deleted content', '', 'lessons', '2026-01-01');
        """
        )
        from infra.fts import _create_fts5_table, cleanup_fts5_orphans

        _create_fts5_table(self.db)
        self._create_fts5_table = _create_fts5_table
        self._cleanup = cleanup_fts5_orphans

    def tearDown(self):
        self.db.close()

    def test_no_orphans_returns_zero(self):
        result = self._cleanup(self.db)
        self.assertEqual(result, 0)

    def test_removes_soft_deleted_notes(self):
        rowid = self.db.execute(
            "INSERT INTO memories(id, content, tags, category, deleted_at) "
            "VALUES ('note/3', 'will delete', '', 'lessons', NULL)"
        ).lastrowid
        self.db.execute("UPDATE memories SET deleted_at='2026-06-01' WHERE id='note/3'")
        self.db.execute(
            "INSERT INTO memories_fts(rowid, id, content, tags, category) "
            "VALUES (?, 'note/3', 'will delete', '', 'lessons')",
            (rowid,),
        )
        n = self._cleanup(self.db)
        self.assertGreater(n, 0)

    def test_removes_orphaned_fts_rows(self):
        self.db.execute(
            "INSERT INTO memories_fts(rowid, id, content, tags, category) "
            "VALUES (9999, 'ghost/1', 'orphaned', '', 'lessons')"
        )
        n = self._cleanup(self.db)
        self.assertGreater(n, 0)
        leftover = self.db.execute(
            "SELECT rowid FROM memories_fts WHERE rowid=9999"
        ).fetchall()
        self.assertEqual(len(leftover), 0)


class TestCreateFts5Table(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT, content TEXT, tags TEXT, category TEXT, deleted_at TEXT
            );
        """
        )

    def tearDown(self):
        self.db.close()

    def test_creates_virtual_table(self):
        from infra.fts import _create_fts5_table

        _create_fts5_table(self.db)
        row = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_creates_sync_triggers(self):
        from infra.fts import _create_fts5_table

        _create_fts5_table(self.db)
        triggers = {
            r[0]
            for r in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        self.assertIn("memories_ai", triggers)
        self.assertIn("memories_ad", triggers)
        self.assertIn("memories_au", triggers)

    def test_calling_twice_is_harmless(self):
        from infra.fts import _create_fts5_table

        _create_fts5_table(self.db)
        _create_fts5_table(self.db)
        triggers = {
            r[0]
            for r in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        self.assertIn("memories_ai", triggers)
        self.assertIn("memories_ad", triggers)
        self.assertIn("memories_au", triggers)


class TestMigratePorterTokenizer(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT, content TEXT, tags TEXT, category TEXT, deleted_at TEXT
            );
        """
        )

    def tearDown(self):
        self.db.close()

    def test_noop_when_porter_exists(self):
        from infra.fts import _create_fts5_table, _migrate_fts5_porter_tokenizer

        _create_fts5_table(self.db)
        _migrate_fts5_porter_tokenizer(self.db)
        row = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("unicode61", (row[0] or "").lower())

    def test_creates_table_if_missing(self):
        from infra.fts import _migrate_fts5_porter_tokenizer

        _migrate_fts5_porter_tokenizer(self.db)
        row = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        ).fetchone()
        self.assertIsNotNone(row)


class TestMigrateEnsureFtsTriggers(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT, content TEXT, tags TEXT, category TEXT, deleted_at TEXT
            );
        """
        )

    def tearDown(self):
        self.db.close()

    def test_creates_missing_triggers(self):
        from infra.fts import _create_fts5_table, _migrate_ensure_fts_triggers

        _create_fts5_table(self.db)
        self.db.execute("DROP TRIGGER memories_ai")
        self.db.execute("DROP TRIGGER memories_ad")
        self.db.execute("DROP TRIGGER memories_au")
        _migrate_ensure_fts_triggers(self.db)
        triggers = {
            r[0]
            for r in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        self.assertIn("memories_ai", triggers)
        self.assertIn("memories_ad", triggers)
        self.assertIn("memories_au", triggers)

    def test_noop_when_triggers_exist(self):
        from infra.fts import _create_fts5_table, _migrate_ensure_fts_triggers

        _create_fts5_table(self.db)
        _migrate_ensure_fts_triggers(self.db)
        triggers = {
            r[0]
            for r in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        self.assertIn("memories_ai", triggers)
        self.assertIn("memories_ad", triggers)
        self.assertIn("memories_au", triggers)

    def test_noop_if_fts_table_missing(self):
        from infra.fts import _migrate_ensure_fts_triggers

        _migrate_ensure_fts_triggers(self.db)
        triggers = {
            r[0]
            for r in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        self.assertEqual(len(triggers), 0)
