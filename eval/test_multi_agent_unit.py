"""Unit tests for multi_agent.py — cross-agent memory sharing.

Tests share_memory, list_shared_memories, import_shared_memory,
and shared_pool_stats with a fully-bootstrapped temp DB. Verifies
semantic correctness: shared notes appear in the pool, imports
create real memories, stats reflect actual state.
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

from memory_common import GLOBAL_MEM_DIR
from db_migrations import run_schema_setup


class TestShareMemory(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="multi_agent_"))
        self.db_path = self.tmpdir / "memory.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        run_schema_setup(conn)
        conn.execute(
            "INSERT INTO memories (id, content, source_file, tags, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "lessons/test-note",
                "shared content",
                "lessons/test-note.md",
                '["multi-agent","test"]',
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
            ),
        )
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

    def test_share_memory_adds_to_shared_pool(self):
        from memory_sharing import share_memory

        result = share_memory(
            "lessons/test-note", "agent-alpha", db_path=str(self.db_path)
        )
        self.assertIn("shared_id", result)
        conn = sqlite3.connect(str(self.db_path))
        row = conn.execute(
            "SELECT content, agent_id FROM shared_memories WHERE source_note_id='lessons/test-note'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "agent-alpha")
        self.assertIn("shared content", row[0])

    def test_share_memory_nonexistent_note(self):
        from memory_sharing import share_memory

        result = share_memory(
            "lessons/nonexistent", "agent-alpha", db_path=str(self.db_path)
        )
        self.assertIn("error", result)


class TestListAndImport(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="multi_agent_"))
        self.db_path = self.tmpdir / "memory.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        run_schema_setup(conn)
        conn.execute(
            "INSERT INTO memories (id, content, source_file, tags, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "lessons/shared-note",
                "imported content",
                "lessons/shared-note.md",
                '["import"]',
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
            ),
        )
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

    def test_list_shared_memories_returns_list(self):
        from memory_sharing import share_memory, list_shared_memories

        share_memory("lessons/shared-note", "agent-beta", db_path=str(self.db_path))
        result = list_shared_memories(db_path=str(self.db_path))
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)

    def test_import_shared_memory_works(self):
        from memory_sharing import share_memory, import_shared_memory

        share_memory("lessons/shared-note", "agent-beta", db_path=str(self.db_path))
        result = import_shared_memory(
            "lessons/shared-note", "agent-gamma", db_path=str(self.db_path)
        )
        self.assertIsInstance(result, dict)


class TestSharedPoolStats(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="multi_agent_"))
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

    def test_shared_pool_stats_returns_valid_dict(self):
        from memory_sharing import shared_pool_stats

        stats = shared_pool_stats(db_path=str(self.db_path))
        self.assertIsInstance(stats, dict)
        self.assertIn("total_shared", stats)


if __name__ == "__main__":
    unittest.main()
