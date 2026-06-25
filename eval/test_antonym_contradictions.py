#!/usr/bin/env python3
"""Unit tests for antonym-aware semantic contradiction detection.

Run with:
    ~/.config/agentic-memory/venv/bin/python -m unittest eval.test_antonym_contradictions -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone

# Make the agentic-memory package importable.
INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from memory_common import open_db, connection_pool, safe_close_db  # noqa: E402
from embedding_search import get_embedding_search  # noqa: E402
from contradiction_detector import detect_contradictions_semantic  # noqa: E402


class TestAntonymContradictions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Warm the singleton model to verify it's available
        cls.es = get_embedding_search()
        if cls.es.model is None:
            raise unittest.SkipTest(
                "model2vec/numpy not available in venv; skipping semantic contradiction tests"
            )

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="antonym_contradiction_test_")
        self.db_path = Path(self.tmpdir) / "memory.db"
        self._bootstrap_db()

    def tearDown(self):
        # Clear connection pool to release file locks on self.db_path
        connection_pool.clear()
        try:
            for p in Path(self.tmpdir).glob("*"):
                p.unlink()
            Path(self.tmpdir).rmdir()
        except Exception:
            pass

    def _bootstrap_db(self):
        with open_db(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    source_file TEXT NOT NULL,
                    tags TEXT DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    pinned INTEGER DEFAULT 0,
                    importance INTEGER DEFAULT 3,
                    decay TEXT DEFAULT 'none',
                    score REAL DEFAULT 1.0,
                    valid_from TEXT,
                    valid_to TEXT,
                    superseded_by TEXT,
                    last_accessed TEXT,
                    metadata TEXT DEFAULT '{}',
                    deleted_at TEXT,
                    deleted_by TEXT
                );
                """
            )
            conn.commit()

    def _insert_note(self, note_id: str, content: str):
        now = datetime.now(timezone.utc).isoformat()
        with open_db(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO memories (id, content, source_file, created_at, updated_at, observed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (note_id, content, f"{note_id}.md", now, now, now),
            )
            conn.commit()

    def test_direct_antonyms_trigger_contradiction(self):
        """Test that fast vs sluggish/slow triggers a semantic_antonym contradiction."""
        self._insert_note("lessons/fast_queries", "The database query execution is fast.")
        self._insert_note("lessons/slow_queries", "The database query execution is sluggish.")

        res = detect_contradictions_semantic(self.tmpdir)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["type"], "semantic_antonym")
        self.assertIn("fast", res[0]["polarity"])
        self.assertIn("sluggish", res[0]["polarity"])

    def test_different_subjects_do_not_trigger(self):
        """Test that antonyms in different contexts do not trigger because of low similarity."""
        self._insert_note("lessons/db_speed", "The database query execution is fast.")
        self._insert_note("lessons/walk_speed", "The user has a slow walking pace.")

        res = detect_contradictions_semantic(self.tmpdir)
        self.assertEqual(len(res), 0)

    def test_synonyms_do_not_trigger(self):
        """Test that semantically identical claims without antonyms do not trigger."""
        self._insert_note("lessons/fast_a", "The database query execution is fast.")
        self._insert_note("lessons/fast_b", "The database query is rapid and fast.")

        res = detect_contradictions_semantic(self.tmpdir)
        self.assertEqual(len(res), 0)

    def test_sync_vs_async_antonym(self):
        """Test that sync vs async triggers a semantic_antonym contradiction."""
        self._insert_note("lessons/sync_mode", "The background queue executes tasks in synchronous mode.")
        self._insert_note("lessons/async_mode", "The background queue executes tasks in asynchronous mode.")

        res = detect_contradictions_semantic(self.tmpdir)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["type"], "semantic_antonym")


if __name__ == "__main__":
    unittest.main()
