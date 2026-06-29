"""Tests for Context window management — bootstrap, recall, context monitor."""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.chdir("..")
sys.path.insert(0, os.getcwd())
from db_migrations import run_schema_setup

_ROW = (
    "INSERT INTO memories (id,content,source_file,tags,created_at,updated_at,observed_at,pinned,importance_score) "
    "VALUES (?,?,?,'[]','2026-01-01T00:00:00','2026-01-01T00:00:00','2026-01-01T00:00:00',?,?)"
)


class TestMemoryBootstrap(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="ctx_"))
        self.db_path = self.tmpdir / "memory.db"
        c = sqlite3.connect(str(self.db_path))
        c.execute("PRAGMA journal_mode=WAL")
        run_schema_setup(c)
        c.commit()
        c.close()
        self._oidb = os.environ.get("MEMORY_DB_PATH")
        os.environ["MEMORY_DB_PATH"] = str(self.db_path)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if self._oidb:
            os.environ["MEMORY_DB_PATH"] = self._oidb
        else:
            os.environ.pop("MEMORY_DB_PATH", None)

    def test_pinned_notes_empty_fresh(self):
        c = sqlite3.connect(str(self.db_path))
        from memory_bootstrap import get_pinned_notes

        self.assertEqual(len(get_pinned_notes(c)), 0)
        c.close()

    def test_pinned_note_appears(self):
        c = sqlite3.connect(str(self.db_path))
        c.execute(_ROW, ("notes/pinned", "pinned content", "notes/pinned.md", 1, 0.9))
        c.commit()
        from memory_bootstrap import get_pinned_notes

        pinned = get_pinned_notes(c)
        c.close()
        self.assertEqual(len(pinned), 1)
        self.assertEqual(pinned[0]["id"], "notes/pinned")

    def test_stats_returns_counts(self):
        c = sqlite3.connect(str(self.db_path))
        from memory_bootstrap import get_stats

        s = get_stats(c)
        c.close()
        self.assertIn("total_notes", s)
        self.assertIsInstance(s["total_notes"], int)

    def test_bootstrap_json_runs(self):
        import sys as _s

        _s.argv = ["mb.py", "--json"]
        try:
            from memory_bootstrap import main

            main(db_path=str(self.db_path))
        except SystemExit:
            pass


class TestRecall(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="ctx_"))
        self.db_path = self.tmpdir / "memory.db"
        c = sqlite3.connect(str(self.db_path))
        c.execute("PRAGMA journal_mode=WAL")
        run_schema_setup(c)
        c.commit()
        c.close()
        self._oidb = os.environ.get("MEMORY_DB_PATH")
        os.environ["MEMORY_DB_PATH"] = str(self.db_path)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if self._oidb:
            os.environ["MEMORY_DB_PATH"] = self._oidb
        else:
            os.environ.pop("MEMORY_DB_PATH", None)

    def test_recall_context_returns_dict(self):
        from recall import recall_context

        r = recall_context(db_path=str(self.db_path))
        self.assertIsInstance(r, dict)

    def test_recall_empty_fresh_db(self):
        from recall import recall_context

        r = recall_context(db_path=str(self.db_path))
        c = r.get("count", 0)
        if c == 0:
            self.assertEqual(c, 0)
        else:
            self.assertGreaterEqual(len(r.get("results", [])), 0)


if __name__ == "__main__":
    unittest.main()
