"""Unit tests for background_worker.py — task handler dispatch and vec drift check.

Tests _check_and_reconcile_vec_drift, task handler dispatch, and
the worker handler registry.
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


class TestVecDriftCheck(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="worker_drift_"))
        self.db_path = self.tmpdir / "memory.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        run_schema_setup(conn)
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_drift_when_tables_empty(self):
        from background_worker import _check_and_reconcile_vec_drift

        conn = sqlite3.connect(str(self.db_path))
        _check_and_reconcile_vec_drift(conn, self.db_path)
        conn.close()

    def test_no_drift_when_in_sync(self):
        from background_worker import _check_and_reconcile_vec_drift

        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO memories (id, content, source_file, tags, created_at, updated_at, observed_at) "
            "VALUES ('test/a', 'hello', 'test/a.md', '[]', '2026-01-01T00:00:00', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        conn.commit()
        _check_and_reconcile_vec_drift(conn, self.db_path)
        conn.close()

    def test_skips_when_tables_missing(self):
        from background_worker import _check_and_reconcile_vec_drift

        conn = sqlite3.connect(":memory:")
        _check_and_reconcile_vec_drift(conn, self.db_path)
        conn.close()


class TestHandlerRegistry(unittest.TestCase):
    def test_all_handlers_registered(self):
        from background_worker import HANDLERS

        self.assertIn("entity_resolution", HANDLERS)
        self.assertIn("fact_consolidation", HANDLERS)
        self.assertIn("vec_index_rebuild", HANDLERS)
        self.assertIn("wal_checkpoint", HANDLERS)

    def test_all_handlers_are_callable(self):
        from background_worker import HANDLERS

        for task_type, handler in HANDLERS.items():
            self.assertTrue(callable(handler), f"{task_type} handler not callable")


class TestDrainMode(unittest.TestCase):
    """Test the --drain flag: process all pending tasks until empty."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="worker_drain_"))
        self.db_path = self.tmpdir / "memory.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        run_schema_setup(conn)
        conn.commit()
        # Add a memory so the entity_resolution task has something to do
        conn.execute(
            "INSERT INTO memories (id, content, source_file, tags, created_at, updated_at, observed_at) "
            "VALUES ('test/drain', 'hello world', 'test/drain.md', '[]', '2026-01-01T00:00:00', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        # Enqueue 3 entity_resolution tasks
        for i in range(3):
            conn.execute(
                "INSERT INTO task_queue (task_type, payload, status) VALUES ('entity_resolution', ?, 'pending')",
                (f'{{"drain_test": {i}}}',),
            )
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_drain_processes_all_pending(self):
        """--drain should process every pending task then exit."""
        from background_worker import run_worker

        run_worker(self.db_path, drain=True, max_tasks=100)
        conn = sqlite3.connect(str(self.db_path))
        pending = conn.execute(
            "SELECT COUNT(*) FROM task_queue WHERE status='pending'"
        ).fetchone()[0]
        completed = conn.execute(
            "SELECT COUNT(*) FROM task_queue WHERE status='completed'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(pending, 0, "drain mode left pending tasks")
        self.assertGreaterEqual(completed, 3, "drain mode did not process all tasks")

    def test_drain_respects_max_tasks(self):
        """--drain --max-tasks=N should stop after N tasks even if more remain."""
        from background_worker import run_worker

        # Enqueue 5 more tasks (so we have 3 + 5 = 8 total pending)
        conn = sqlite3.connect(str(self.db_path))
        for i in range(5):
            conn.execute(
                "INSERT INTO task_queue (task_type, payload, status) VALUES ('entity_resolution', ?, 'pending')",
                (f'{{"cap_test": {i}}}',),
            )
        conn.commit()
        # Verify we have 8 pending
        n_pending = conn.execute(
            "SELECT COUNT(*) FROM task_queue WHERE status='pending'"
        ).fetchone()[0]
        self.assertEqual(n_pending, 8, "setup: expected 8 pending tasks")
        conn.close()

        # Drain with cap of 3 — only 3 of the 8 should be completed
        run_worker(self.db_path, drain=True, max_tasks=3)
        conn = sqlite3.connect(str(self.db_path))
        completed = conn.execute(
            "SELECT COUNT(*) FROM task_queue WHERE status='completed'"
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM task_queue WHERE status='pending'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(completed, 3, f"expected 3 completed, got {completed}")
        self.assertEqual(pending, 5, f"expected 5 remaining pending, got {pending}")

    def test_drain_handles_empty_queue(self):
        """--drain on an empty queue should be a no-op (not error)."""
        from background_worker import run_worker

        conn = sqlite3.connect(str(self.db_path))
        # Clear all tasks
        conn.execute("DELETE FROM task_queue")
        conn.commit()
        conn.close()

        # Should not raise
        run_worker(self.db_path, drain=True, max_tasks=10)


class TestWalCheckpointHandler(unittest.TestCase):
    """S4.7: tests for the wal_checkpoint handler and debounced runner."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="worker_wal_"))
        self.db_path = self.tmpdir / "memory.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        run_schema_setup(conn)
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_handler_returns_status_string(self):
        from background_worker import handle_wal_checkpoint

        conn = sqlite3.connect(str(self.db_path))
        result = handle_wal_checkpoint({"threshold_mb": 0.0}, conn, self.db_path)
        conn.close()
        # Empty DB → skipped (because WAL is below 0.0 MB threshold? or
        # because there's no parent dir). The result must be a string.
        self.assertIsInstance(result, str)
        self.assertIn("wal_checkpoint", result)

    def test_handler_skips_on_missing_path(self):
        """A missing path is not an error — ``wal_checkpoint_idle``
        returns ``status="skipped"`` with ``ok=False``.  The handler
        just wraps that into a string."""
        from background_worker import handle_wal_checkpoint

        conn = sqlite3.connect(":memory:")
        result = handle_wal_checkpoint(
            {"threshold_mb": 0.0},
            conn,
            Path("/nonexistent/ghost/memory.db"),
        )
        conn.close()
        self.assertIsInstance(result, str)
        self.assertIn("skipped", result)

    def test_debounce_skips_within_60s(self):
        """S4.4: a second call within 60s of the first must not
        re-run the checkpoint.  We test by calling
        _maybe_run_wal_checkpoint twice and checking the
        timestamp only advanced once."""
        from background_worker import (
            _maybe_run_wal_checkpoint,
        )
        import background_worker

        # Reset state
        background_worker._last_wal_checkpoint_at = 0.0
        # 0 interval → must always run (no time-based skip)
        # 0 threshold → must always run (no size-based skip)
        old_interval = os.environ.get("MEMORY_WAL_CHECKPOINT_INTERVAL_S")
        os.environ["MEMORY_WAL_CHECKPOINT_INTERVAL_S"] = "0"
        try:
            conn = sqlite3.connect(str(self.db_path))
            _maybe_run_wal_checkpoint(conn, self.db_path)
            ts_after_first = background_worker._last_wal_checkpoint_at
            self.assertGreater(ts_after_first, 0.0)

            # Second call within 60s must NOT update timestamp.
            _maybe_run_wal_checkpoint(conn, self.db_path)
            ts_after_second = background_worker._last_wal_checkpoint_at
            self.assertEqual(ts_after_first, ts_after_second)
            conn.close()
        finally:
            if old_interval is None:
                os.environ.pop("MEMORY_WAL_CHECKPOINT_INTERVAL_S", None)
            else:
                os.environ["MEMORY_WAL_CHECKPOINT_INTERVAL_S"] = old_interval


class TestMmapSizeConfig(unittest.TestCase):
    """S4.7: mmap_size is applied via PRAGMA and respects env override."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="mmap_test_"))
        self.db_path = self.tmpdir / "memory.db"

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_resolve_mmap_size_default(self):
        from infra.db import _resolve_mmap_size

        old = os.environ.pop("MEMORY_SQLITE_MMAP_SIZE", None)
        try:
            val = _resolve_mmap_size()
            self.assertEqual(val, 268_435_456)
        finally:
            if old is not None:
                os.environ["MEMORY_SQLITE_MMAP_SIZE"] = old

    def test_resolve_mmap_size_env_zero(self):
        from infra.db import _resolve_mmap_size

        old = os.environ.get("MEMORY_SQLITE_MMAP_SIZE")
        os.environ["MEMORY_SQLITE_MMAP_SIZE"] = "0"
        try:
            self.assertEqual(_resolve_mmap_size(), 0)
        finally:
            if old is None:
                os.environ.pop("MEMORY_SQLITE_MMAP_SIZE", None)
            else:
                os.environ["MEMORY_SQLITE_MMAP_SIZE"] = old

    def test_resolve_mmap_size_env_override(self):
        from infra.db import _resolve_mmap_size

        old = os.environ.get("MEMORY_SQLITE_MMAP_SIZE")
        os.environ["MEMORY_SQLITE_MMAP_SIZE"] = "134217728"
        try:
            self.assertEqual(_resolve_mmap_size(), 134_217_728)
        finally:
            if old is None:
                os.environ.pop("MEMORY_SQLITE_MMAP_SIZE", None)
            else:
                os.environ["MEMORY_SQLITE_MMAP_SIZE"] = old

    def test_resolve_mmap_size_env_garbage_falls_back(self):
        from infra.db import _resolve_mmap_size

        old = os.environ.get("MEMORY_SQLITE_MMAP_SIZE")
        os.environ["MEMORY_SQLITE_MMAP_SIZE"] = "not-a-number"
        try:
            # Invalid value falls back to 256 MiB default
            self.assertEqual(_resolve_mmap_size(), 268_435_456)
        finally:
            if old is None:
                os.environ.pop("MEMORY_SQLITE_MMAP_SIZE", None)
            else:
                os.environ["MEMORY_SQLITE_MMAP_SIZE"] = old

    def test_mmap_size_applied_to_connection(self):
        """S4.7: confirm mmap_size PRAGMA is applied when opening
        a connection via the pool.  Use a temp DB."""
        import threading
        from infra.db import connection_pool

        old = os.environ.get("MEMORY_SQLITE_MMAP_SIZE")
        os.environ["MEMORY_SQLITE_MMAP_SIZE"] = "1048576"  # 1 MiB
        try:
            conn = connection_pool.get(str(self.db_path), timeout=5.0)
            try:
                row = conn.execute("PRAGMA mmap_size").fetchone()
                self.assertEqual(row[0], 1_048_576)
            finally:
                connection_pool.put(conn)
        finally:
            if old is None:
                os.environ.pop("MEMORY_SQLITE_MMAP_SIZE", None)
            else:
                os.environ["MEMORY_SQLITE_MMAP_SIZE"] = old
            # Evict from pool so next test gets a fresh conn
            key = (str(self.db_path), threading.get_ident())
            connection_pool._pool.pop(key, None)
            try:
                connection_pool._lru.remove(key)
            except ValueError:
                pass


if __name__ == "__main__":
    unittest.main()
