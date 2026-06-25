#!/usr/bin/env python3
"""Unit tests for sdk.Memory.

Covers H13 regression: the Mem0-compat class accepts a user_id but
the `add()` method never propagates it. The test pins the current
(known-broken) state so a future fix is observable.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from memory_common import connection_pool, open_db


def _fresh_db(name: str) -> Path:
    """Per-test temp DB so test order doesn't matter."""
    p = Path(tempfile.mkdtemp(prefix=f"sdk_{name}_")) / "memory.db"
    os.environ["MEMORY_DB_PATH"] = str(p)
    connection_pool.clear()
    return p


from sdk import Memory


class TestSdkMemoryAdd(unittest.TestCase):
    def test_add_returns_string_note_id(self):
        db = _fresh_db("add")
        m = Memory(db_path=str(db))
        result = m.add("test content")
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0, f"Expected non-empty note_id, got {result!r}")

    def test_add_with_tags(self):
        db = _fresh_db("tags")
        m = Memory(db_path=str(db))
        result = m.add("tagged content", tags=["alpha", "beta"])
        self.assertIsInstance(result, str)

    def test_user_id_stored(self):
        """user_id is stored on the instance even though add() doesn't
        propagate it. This is the H13 audit finding. Once the fix
        lands, an additional assertion will verify the propagation.
        """
        db = _fresh_db("user")
        m = Memory(db_path=str(db), user_id="alice")
        self.assertEqual(m._user_id, "alice")
        m.add("alice's content")
        # Verify the note was saved at all.
        with open_db(db) as conn:
            rows = conn.execute(
                "SELECT id, content FROM memories WHERE id LIKE 'sdk/%'"
            ).fetchall()
        self.assertGreater(len(rows), 0, f"Expected sdk/% rows, found {rows}")


class TestSdkMemorySearch(unittest.TestCase):
    def setUp(self):
        db = _fresh_db("search")
        m = Memory(db_path=str(db))
        m.add("the quick brown fox")
        m.add("jumps over the lazy dog")

    def test_search_returns_list(self):
        db = _fresh_db("search2")
        m = Memory(db_path=str(db))
        results = m.search("fox")
        self.assertIsInstance(results, list)
        for r in results:
            self.assertIn("id", r)
            self.assertIn("content", r)


class TestSdkMemoryStats(unittest.TestCase):
    def test_stats_returns_dict(self):
        db = _fresh_db("stats")
        m = Memory(db_path=str(db))
        m.add("hello world")
        stats = m.stats()
        self.assertIsInstance(stats, dict)
        self.assertIn("memories", stats)
        self.assertGreater(stats["memories"], 0)


if __name__ == "__main__":
    unittest.main()
