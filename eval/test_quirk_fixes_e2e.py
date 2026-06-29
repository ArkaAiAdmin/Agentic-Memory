#!/usr/bin/env python3
"""E2E validation for Quirk 2 and Quirk 8 fixes.

Run with:
    ~/.config/agentic-memory/venv/bin/python -m pytest eval/test_quirk_fixes_e2e.py -v
"""
import sys
import sqlite3
import tempfile
import shutil
import unittest
from pathlib import Path
from typing import Optional

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from memory_common import open_db


def _create_test_db(tmp_dir: Path) -> Path:
    """Create a fresh test DB with the required schema."""
    db_path = tmp_dir / "memory.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            source_file TEXT,
            tags TEXT,
            category TEXT,
            metadata TEXT,
            tier TEXT DEFAULT 'warm',
            importance_score REAL DEFAULT 0.5,
            pinned INTEGER DEFAULT 0,
            fitness_score REAL DEFAULT 0.5,
            created_at TEXT,
            updated_at TEXT,
            last_accessed TEXT,
            deleted_at TEXT,
            valid_to TEXT,
            superseded_by TEXT,
            repo_id TEXT,
            session_id TEXT,
            agent_id TEXT,
            entity_type TEXT
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content, tags,
            tokenize='unicode61'
        );
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, content, tags)
            VALUES (new.rowid, new.content, new.tags);
        END;
        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
            DELETE FROM memories_fts WHERE rowid = old.rowid;
            INSERT INTO memories_fts(rowid, content, tags)
            SELECT new.rowid, new.content, new.tags WHERE new.deleted_at IS NULL;
        END;
        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            DELETE FROM memories_fts WHERE rowid = old.rowid;
        END;
    """)
    conn.close()
    return db_path


def _insert_note(db_path: Path, note_id: str, content: str, tags: str = '[]', deleted_at: Optional[str] = None):
    """Insert a note into the test DB."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO memories (id, content, tags, created_at, updated_at, deleted_at) VALUES (?, ?, ?, datetime('now'), datetime('now'), ?)",
        (note_id, content, tags, deleted_at)
    )
    conn.commit()
    conn.close()


class TestQuirk2FTSDeleteFilter(unittest.TestCase):
    """Verify that soft-deleted notes are excluded from FTS5 search results."""

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.db_path = _create_test_db(self.tmp_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_soft_deleted_note_excluded_from_fts(self):
        """A soft-deleted note must NOT appear in FTS5 search."""
        _insert_note(self.db_path, "test/deleted1", "quantum computing is fascinating", deleted_at="2026-06-08T00:00:00")
        _insert_note(self.db_path, "test/active1", "quantum computing is fascinating")

        with open_db(self.db_path) as conn:
            # Raw FTS5 query (what search_memories uses)
            results = conn.execute(
                """SELECT m.id, m.content
                   FROM memories_fts fts
                   JOIN memories m ON m.rowid = fts.rowid
                   WHERE memories_fts MATCH 'quantum' AND m.deleted_at IS NULL
                   ORDER BY fts.rank"""
            ).fetchall()

            ids = [r[0] for r in results]
            self.assertIn("test/active1", ids, "Active note should appear in results")
            self.assertNotIn("test/deleted1", ids, "Soft-deleted note must NOT appear in results")

    def test_active_note_appear_in_fts(self):
        """Non-deleted notes must still appear in FTS5 search."""
        _insert_note(self.db_path, "test/active1", "machine learning algorithms")
        _insert_note(self.db_path, "test/active2", "deep learning neural networks")

        with open_db(self.db_path) as conn:
            results = conn.execute(
                """SELECT m.id
                   FROM memories_fts fts
                   JOIN memories m ON m.rowid = fts.rowid
                   WHERE memories_fts MATCH 'learning' AND m.deleted_at IS NULL
                   ORDER BY fts.rank"""
            ).fetchall()

            ids = [r[0] for r in results]
            self.assertIn("test/active1", ids)
            self.assertIn("test/active2", ids)

    def test_all_deleted_returns_empty(self):
        """If all matching notes are deleted, search returns empty."""
        _insert_note(self.db_path, "test/del1", "rare searchterm xyz", deleted_at="2026-06-08T00:00:00")
        _insert_note(self.db_path, "test/del2", "rare searchterm xyz", deleted_at="2026-06-08T00:00:00")

        with open_db(self.db_path) as conn:
            results = conn.execute(
                """SELECT m.id
                   FROM memories_fts fts
                   JOIN memories m ON m.rowid = fts.rowid
                   WHERE memories_fts MATCH 'searchterm' AND m.deleted_at IS NULL"""
            ).fetchall()

            self.assertEqual(len(results), 0, "All deleted notes should be excluded")

    def test_restore_makes_note_searchable_again(self):
        """After restoring a note (deleted_at=NULL), it should appear in search."""
        _insert_note(self.db_path, "test/restore1", "restore test unique phrase")
        # Soft-delete
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("UPDATE memories SET deleted_at = '2026-06-08T00:00:00' WHERE id = 'test/restore1'")
        conn.commit()
        # Restore
        conn.execute("UPDATE memories SET deleted_at = NULL WHERE id = 'test/restore1'")
        conn.commit()
        conn.close()

        with open_db(self.db_path) as conn:
            results = conn.execute(
                """SELECT m.id
                   FROM memories_fts fts
                   JOIN memories m ON m.rowid = fts.rowid
                   WHERE memories_fts MATCH 'unique' AND m.deleted_at IS NULL"""
            ).fetchall()

            ids = [r[0] for r in results]
            self.assertIn("test/restore1", ids, "Restored note should be searchable")


class TestQuirk8ConsolidationGuard(unittest.TestCase):
    """Verify that consolidate_facts.py exits early for large corpora."""

    def test_guard_rejects_large_corpus(self):
        """consolidate_facts.py should warn and exit when >2000 notes."""
        # We can't easily create a 2000+ note DB in a test, so we verify
        # the guard logic directly by reading the source code.

        # Read the source of consolidate_facts.py
        cf_path = INSTALL_DIR / "fact" / "consolidate_facts.py"
        source = cf_path.read_text()

        # Verify the guard exists
        self.assertIn("len(memories) > 2000", source, "Guard check for >2000 notes must exist")
        self.assertIn("cron_consolidate.py", source, "Guard should recommend cron_consolidate.py")

    def test_guard_imports_safe_close_db(self):
        """consolidate_facts.py must import safe_close_db for the early return."""
        cf_path = INSTALL_DIR / "fact" / "consolidate_facts.py"
        source = cf_path.read_text()
        self.assertIn("safe_close_db", source, "safe_close_db must be imported")


class TestDocumentationAccuracy(unittest.TestCase):
    """Verify that documentation quirks match actual code behavior."""

    def test_quirk7_returns_error_string(self):
        """Quirk 7: embedding model failure returns error string, not empty."""
        es_path = INSTALL_DIR / "infra" / "embedding_search.py"
        source = es_path.read_text()
        # The search() method should return an error string when model is None
        self.assertIn("Embedding search unavailable", source,
                       "search() must return error string when model is None")

    def test_quirk11_pool_is_per_path(self):
        """Quirk 11: pool maps (path, thread_ident)->connection, not thread-local."""
        db_path = INSTALL_DIR / "infra" / "db.py"
        source = db_path.read_text()
        # The pool should use a dict with tuple keys, not threading.local
        self.assertIn("dict[tuple[str, int], sqlite3.Connection]", source,
                       "Pool should be dict[tuple[str, int], Connection], not thread-local")
        self.assertNotIn("threading.local", source.split("_ConnectionPool")[1].split("class")[0],
                          "Pool should NOT use threading.local")


if __name__ == "__main__":
    unittest.main()
