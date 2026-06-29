"""Tests for Synchronization layer — CRDT, sync invariants, concurrent writes."""

import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.chdir("..")
sys.path.insert(0, os.getcwd())
from db_migrations import run_schema_setup

_ROW = (
    "INSERT INTO memories (id,content,source_file,tags,created_at,updated_at,observed_at) "
    "VALUES (?,?,?,'[]','2026-01-01T00:00:00','2026-01-01T00:00:00','2026-01-01T00:00:00')"
)


class TestSyncInvariant(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="sync_"))
        self.db_path = self.tmpdir / "memory.db"
        c = sqlite3.connect(str(self.db_path))
        c.execute("PRAGMA journal_mode=WAL")
        run_schema_setup(c)
        c.commit()
        c.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_check_sync_invariant_returns_dict(self):
        from sync_invariant import check_sync_invariant

        c = sqlite3.connect(str(self.db_path))
        r = check_sync_invariant(c)
        c.close()
        self.assertIsInstance(r, dict)
        self.assertIn("overall", r)

    def test_empty_db_reports_empty(self):
        from sync_invariant import check_sync_invariant

        c = sqlite3.connect(str(self.db_path))
        r = check_sync_invariant(c)
        c.close()
        self.assertIn(r["overall"], ("empty", "healthy"))

    def test_get_drifted_subsystems_returns_list(self):
        from sync_invariant import check_sync_invariant, get_drifted_subsystems

        c = sqlite3.connect(str(self.db_path))
        r = check_sync_invariant(c)
        d = get_drifted_subsystems(r)
        c.close()
        self.assertIsInstance(d, list)

    def test_sync_invariant_after_insert(self):
        c = sqlite3.connect(str(self.db_path))
        c.execute(_ROW, ("test/a", "sync test", "test/a.md"))
        c.commit()
        from sync_invariant import check_sync_invariant

        r = check_sync_invariant(c)
        c.close()
        self.assertIsInstance(r, dict)  # check runs without error


class TestConcurrentWriteSafety(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="sync_"))
        self.db_path = self.tmpdir / "memory.db"
        c = sqlite3.connect(str(self.db_path))
        c.execute("PRAGMA journal_mode=WAL")
        run_schema_setup(c)
        c.commit()
        c.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_wal_mode_enabled(self):
        c = sqlite3.connect(str(self.db_path))
        self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        c.close()

    def test_concurrent_readers(self):
        results = []

        def read_db():
            c = sqlite3.connect(str(self.db_path))
            c.execute("PRAGMA journal_mode=WAL")
            results.append(c.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            c.close()

        t1, t2 = threading.Thread(target=read_db), threading.Thread(target=read_db)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(len(results), 2)


if __name__ == "__main__":
    unittest.main()
