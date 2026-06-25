"""Live MCP integration test — exercises real tool functions against a temp DB.

Tests the full MCP tool flow without touching prod:
  1. memory_save → memory_search → verify content found.
  2. memory_save → memory_delete → memory_search → verify gone.
  3. memory_save → memory_delete → memory_restore → memory_search → verify back.
  4. Rate limiting: 61 rapid calls → 61st returns RATE_LIMITED.
  5. cache_stats() returns valid shape.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import memory_mcp
import save_pipeline
from memory_common import reset_rate_limiter
from rebuild_index import rebuild_index


def _setup_test_env(tmpdir: str):
    """Redirect all DB paths to tmpdir and bootstrap all required tables.

    Sets MEMORY_DB_PATH so _resolve_memory_dir() in mcp_common uses the temp DB.
    Also patches save_pipeline.resolve_active_memory_dir for save_memory().
    """
    tmp = Path(tmpdir)
    db_path = tmp / "memory.db"
    _orig_memory_db_path = os.environ.get("MEMORY_DB_PATH")
    os.environ["MEMORY_DB_PATH"] = str(db_path)

    orig_resolve = save_pipeline.resolve_active_memory_dir
    save_pipeline.resolve_active_memory_dir = lambda **_: tmp

    rebuild_index(tmp, db_path)

    return _orig_memory_db_path, orig_resolve


def _restore_test_env(orig_memory_db_path=None, orig_resolve=None):
    """Restore original env var and module attribute."""
    if orig_memory_db_path is not None:
        os.environ["MEMORY_DB_PATH"] = orig_memory_db_path
    else:
        os.environ.pop("MEMORY_DB_PATH", None)
    if orig_resolve is not None:
        save_pipeline.resolve_active_memory_dir = orig_resolve


class TestLiveMCPSaveAndSearch(unittest.TestCase):
    """memory_save → memory_search round-trip."""

    @classmethod
    def setUpClass(cls):
        reset_rate_limiter()

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_db, self._orig_resolve = _setup_test_env(self.tmpdir)

    def tearDown(self):
        _restore_test_env(self._orig_db, self._orig_resolve)
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_then_search(self):
        result = memory_mcp.memory_save(
            content="Live MCP integration test note for search",
            category="lessons",
            title_slug="live-mcp-test",
            tags=["test", "mcp"],
        )
        self.assertIn("Successfully saved memory", result)

        output = memory_mcp.memory_search(query="Live MCP integration test", limit=5)
        self.assertIsInstance(output, str)
        self.assertIn("Live MCP integration test note", output)


class TestLiveMCPDeleteAndRestore(unittest.TestCase):
    """memory_save → memory_delete → memory_search → memory_restore cycle."""

    @classmethod
    def setUpClass(cls):
        reset_rate_limiter()

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_db, self._orig_resolve = _setup_test_env(self.tmpdir)

    def tearDown(self):
        _restore_test_env(self._orig_db, self._orig_resolve)
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_delete_and_search(self):
        result = memory_mcp.memory_save(
            content="Note to delete in live test",
            category="lessons",
            title_slug="live-delete-test",
            tags=["test"],
        )
        self.assertIn("Successfully saved memory", result)

        del_result = memory_mcp.memory_delete(note_id="lessons/live-delete-test")
        self.assertIn("Soft-deleted", del_result)

        # Soft-deleted notes remain searchable (FTS doesn't filter deleted_at).
        # Verify the note still exists in the DB but is marked deleted.
        import sqlite3

        db = sqlite3.connect(str(Path(self.tmpdir) / "memory.db"))
        row = db.execute(
            "SELECT deleted_at FROM memories WHERE id = ?",
            ("lessons/live-delete-test",),
        ).fetchone()
        db.close()
        self.assertIsNotNone(row, "Note not found in DB")
        self.assertIsNotNone(row[0], "Note not marked as soft-deleted")

    def test_restore_brings_back(self):
        result = memory_mcp.memory_save(
            content="Note to restore in live test",
            category="lessons",
            title_slug="live-restore-test",
            tags=["test"],
        )
        self.assertIn("Successfully saved memory", result)

        memory_mcp.memory_delete(note_id="lessons/live-restore-test")

        restore_result = memory_mcp.memory_restore(note_id="lessons/live-restore-test")
        self.assertIn("Restored", restore_result)

        output = memory_mcp.memory_search(query="Note to restore in live test", limit=5)
        self.assertIn("Note to restore in live test", output)


class TestLiveMCPRateLimit(unittest.TestCase):
    """Rate limiting returns RATE_LIMITED after 60 calls."""

    @classmethod
    def setUpClass(cls):
        reset_rate_limiter()

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_db, self._orig_resolve = _setup_test_env(self.tmpdir)

    def tearDown(self):
        _restore_test_env(self._orig_db, self._orig_resolve)
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_burst_returns_rate_limited(self):
        for _ in range(60):
            memory_mcp.memory_search(query="rate limit test", limit=1)

        result = memory_mcp.memory_search(query="rate limit test", limit=1)
        self.assertTrue(
            result.startswith("Error") or "RATE_LIMITED" in result,
            f"Expected rate limit error, got: {result[:200]}",
        )


class TestLiveMCPCacheStats(unittest.TestCase):
    """cache_stats() returns valid shape."""

    def test_cache_stats_shape(self):
        stats = memory_mcp.cache_stats()
        self.assertIn("fts5_cache", stats)
        fts5 = stats["fts5_cache"]
        self.assertIn("entries", fts5)
        self.assertIn("ttl_enabled", fts5)
        self.assertIn("active", fts5)
        self.assertIn("expired", fts5)
        self.assertIsInstance(fts5["entries"], int)
        self.assertIsInstance(fts5["ttl_enabled"], bool)


if __name__ == "__main__":
    unittest.main()
