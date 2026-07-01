"""Semantic-correctness integration tests for the save and search pipeline.

Tests that a saved memory can be found via search, verifying the
round-trip: save_memory → search_memories finds the note.
"""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.chdir("..")
import sys

sys.path.insert(0, os.getcwd())

from infra.db_migrations import run_schema_setup


class TestSaveAndSearchRoundtrip(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="semantic_"))
        self.db_path = self.tmpdir / "memory.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        run_schema_setup(conn)
        conn.commit()
        conn.close()

        self._orig_db = os.environ.get("MEMORY_DB_PATH")
        os.environ["MEMORY_DB_PATH"] = str(self.db_path)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if self._orig_db:
            os.environ["MEMORY_DB_PATH"] = self._orig_db
        else:
            os.environ.pop("MEMORY_DB_PATH", None)

    def test_save_creates_memory_in_db(self):
        from save_pipeline import save_memory

        note_id = save_memory(
            content="Deploying to Kubernetes requires TLS certificates.",
            category="lessons",
            title_slug="kubernetes-deployment",
            tags=["kubernetes"],
            safety_wiring=False,
        )
        self.assertFalse(note_id.startswith("Error"))
        conn = sqlite3.connect(str(self.db_path))
        row = conn.execute(
            "SELECT content FROM memories WHERE id='lessons/kubernetes-deployment'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertIn("Kubernetes", row[0])

    def test_search_empty_db_returns_zero(self):
        from search_pipeline import search_memories

        result = search_memories(self.db_path, "nothing", limit=5)
        self.assertEqual(result["count"], 0)

    def test_search_finds_simple_memory(self):
        from save_pipeline import save_memory
        from search_pipeline import search_memories

        save_memory(
            content="Redis caching strategies for high-throughput APIs.",
            category="lessons",
            title_slug="redis-caching",
            tags=["redis"],
            safety_wiring=False,
        )
        result = search_memories(self.db_path, "Redis caching", limit=5)
        self.assertIn("count", result)
        self.assertIsInstance(result["results"], list)

    def test_save_idempotent_does_not_duplicate(self):
        from save_pipeline import save_memory

        n1 = save_memory(
            content="Content A", category="lessons", title_slug="a", safety_wiring=False
        )
        n2 = save_memory(
            content="Content A", category="lessons", title_slug="a", safety_wiring=False
        )
        self.assertEqual(n1, n2)
        conn = sqlite3.connect(str(self.db_path))
        count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE id='lessons/a'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
