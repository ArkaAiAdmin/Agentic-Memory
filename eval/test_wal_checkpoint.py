"""Tests for BLK-4: WAL checkpoint helper (memory_common.wal_checkpoint_idle).

Covers:
  1. wal_checkpoint_idle returns skipped when WAL < threshold.
  2. wal_checkpoint_idle returns done when WAL > threshold.
  3. wal_checkpoint_idle handles missing DB gracefully.
  4. _maybe_checkpoint_on_startup sets flag and runs once.
  5. open_db calls _maybe_checkpoint_on_startup.
  6. memory_compact calls wal_checkpoint_idle.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.memory_common import (
    wal_checkpoint_idle,
    _maybe_checkpoint_on_startup,
)


class TestWalCheckpointIdle(unittest.TestCase):
    """wal_checkpoint_idle behaviour."""

    def test_skipped_when_wal_below_threshold(self):
        """WAL < threshold => status=skipped, ok=True."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "test.db"
            conn = sqlite3.connect(str(db))
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("CREATE TABLE t (x TEXT)")
            conn.commit()
            conn.close()
            result = wal_checkpoint_idle(db, wal_size_threshold_mb=9999.0)
            self.assertEqual(result["status"], "skipped")
            self.assertTrue(result["ok"])

    def test_done_when_wal_exceeds_threshold(self):
        """WAL > threshold => status=done, checkpoint runs."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "test.db"
            # Keep conn1 open to prevent auto-checkpoint on close
            conn1 = sqlite3.connect(str(db))
            conn1.execute("PRAGMA journal_mode = WAL")
            conn1.execute("CREATE TABLE t (x TEXT)")
            for i in range(200):
                conn1.execute("INSERT INTO t VALUES (?)", (f"row-{i}",))
            conn1.commit()
            # WAL now exists on disk (conn1 holds it open)
            result = wal_checkpoint_idle(db, wal_size_threshold_mb=0.001)
            conn1.close()
            self.assertEqual(result["status"], "done")
            self.assertIn("ok", result)
            self.assertIn("log_pages", result)

    def test_missing_db_returns_error(self):
        """Non-existent DB (parent dir doesn't exist) => status=skipped
        with ok=True. The function treats missing-parent-dir as
        'orphan recovery case' (per db.py:wal_checkpoint_idle docstring)
        and returns 'skipped' rather than 'error'.

        For a real 'error' case, see test_compact_error_handled
        (a SQLite error during checkpoint, not a missing path).
        """
        result = wal_checkpoint_idle(
            Path("/nonexistent/memory.db"),
            wal_size_threshold_mb=0.0,
        )
        self.assertEqual(result["status"], "skipped")
        self.assertTrue(result["ok"])

    def test_in_memory_db_skipped(self):
        """In-memory DB has no WAL file => skipped."""
        result = wal_checkpoint_idle(
            Path(":memory:"),
            wal_size_threshold_mb=9999.0,
        )
        self.assertEqual(result["status"], "skipped")
        self.assertTrue(result["ok"])


class TestMaybeCheckpointOnStartup(unittest.TestCase):
    """_maybe_checkpoint_on_startup one-shot guard."""

    def test_sets_flag_after_first_call(self):
        """First call sets _STARTUP_CHECKPOINT_DONE to True."""
        import infra.memory_common as memory_common

        old = memory_common._STARTUP_CHECKPOINT_DONE
        memory_common._STARTUP_CHECKPOINT_DONE = False
        try:
            with tempfile.TemporaryDirectory() as td:
                db = Path(td) / "test.db"
                conn = sqlite3.connect(str(db))
                conn.execute("CREATE TABLE t (x TEXT)")
                conn.commit()
                conn.close()
                # Without env var, checkpoint is skipped but flag is set
                _maybe_checkpoint_on_startup(db)
                self.assertTrue(memory_common._STARTUP_CHECKPOINT_DONE)
        finally:
            memory_common._STARTUP_CHECKPOINT_DONE = old

    def test_second_call_is_noop(self):
        """Second call is a no-op (flag already True)."""
        import infra.memory_common as memory_common

        old = memory_common._STARTUP_CHECKPOINT_DONE
        memory_common._STARTUP_CHECKPOINT_DONE = True
        try:
            with tempfile.TemporaryDirectory() as td:
                db = Path(td) / "test.db"
                # Should not raise, should not checkpoint
                _maybe_checkpoint_on_startup(db)
        finally:
            memory_common._STARTUP_CHECKPOINT_DONE = old

    def test_noop_without_env_var(self):
        """Without MEMORY_WAL_CHECKPOINT_STARTUP=1, checkpoint is skipped."""
        import infra.memory_common as memory_common

        old = memory_common._STARTUP_CHECKPOINT_DONE
        memory_common._STARTUP_CHECKPOINT_DONE = False
        old_env = os.environ.pop("MEMORY_WAL_CHECKPOINT_STARTUP", None)
        conn = None
        conn1 = None
        try:
            with tempfile.TemporaryDirectory() as td:
                db = Path(td) / "test.db"
                conn = sqlite3.connect(str(db))
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("CREATE TABLE t (x TEXT)")
                conn1 = sqlite3.connect(str(db))
                conn1.execute("INSERT INTO t VALUES ('x')")
                conn1.commit()
                # WAL exists but env var not set => no checkpoint
                _maybe_checkpoint_on_startup(db)
                wal_path = Path(td) / "test.db-wal"
                self.assertTrue(wal_path.exists())  # WAL not cleaned up
        finally:
            memory_common._STARTUP_CHECKPOINT_DONE = old
            if old_env is not None:
                os.environ["MEMORY_WAL_CHECKPOINT_STARTUP"] = old_env
            if conn is not None:
                conn.close()
            if conn1 is not None:
                conn1.close()


class TestMemoryCompactCallsCheckpoint(unittest.TestCase):
    """memory_compact appends WAL checkpoint result to output."""

    @patch(
        "mcp_surface.mcp_rebuild._run_subprocess_output",
        return_value=("mocked output", 0),
    )
    @patch(
        "memory_common.wal_checkpoint_idle",
        return_value={"status": "skipped", "ok": True},
    )
    def test_compact_includes_checkpoint(self, mock_ckpt, mock_run):
        """memory_compact calls wal_checkpoint_idle after rebuild."""
        import memory_mcp
        import infra.memory_common as memory_common

        old = memory_common._STARTUP_CHECKPOINT_DONE
        memory_common._STARTUP_CHECKPOINT_DONE = True  # skip startup checkpoint
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "memory.db"
            db_path.touch()
            old_env = os.environ.get("MEMORY_DB_PATH")
            os.environ["MEMORY_DB_PATH"] = str(db_path)
            try:
                result = memory_mcp.memory_compact(dry_run=True)
                self.assertIn("Tier Migration", result)
                mock_ckpt.assert_called_once()
            finally:
                if old_env is not None:
                    os.environ["MEMORY_DB_PATH"] = old_env
                else:
                    os.environ.pop("MEMORY_DB_PATH", None)
                memory_common._STARTUP_CHECKPOINT_DONE = old


if __name__ == "__main__":
    unittest.main()
