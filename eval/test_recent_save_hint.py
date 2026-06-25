#!/usr/bin/env python3
"""Unit tests for recent_save_hint.py.

Run with:
    ~/.config/agentic-memory/venv/bin/python eval/test_recent_save_hint.py
"""

import sys
import time
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))
sys.path.insert(0, str(INSTALL_DIR / "eval"))

from recent_save_hint import note_saved, recent_save_for, clear, RECENT_SAVE_TTL_S
from _wait_until import wait_until  # noqa: E402


class TestRecentSaveHint(unittest.TestCase):
    def setUp(self):
        clear()

    def test_note_saved_then_found(self):
        note_saved("note-1", "/tmp/test.db")
        result = recent_save_for("/tmp/test.db")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "note-1")

    def test_multiple_paths_isolated(self):
        note_saved("note-a", "/tmp/db1.db")
        note_saved("note-b", "/tmp/db2.db")
        r1 = recent_save_for("/tmp/db1.db")
        r2 = recent_save_for("/tmp/db2.db")
        self.assertEqual(r1[0], "note-a")
        self.assertEqual(r2[0], "note-b")

    def test_empty_db_path_returns_none(self):
        note_saved("note-1", "/tmp/test.db")
        result = recent_save_for("")
        self.assertIsNone(result)

    def test_empty_note_id_is_noop(self):
        note_saved("", "/tmp/test.db")
        result = recent_save_for("/tmp/test.db")
        self.assertIsNone(result)

    def test_ttl_expiry(self):
        original_ttl = RECENT_SAVE_TTL_S
        try:
            import recent_save_hint as rsh

            rsh.RECENT_SAVE_TTL_S = 0.05
            note_saved("old-note", "/tmp/test.db")
            # Wait for the 0.05s TTL to expire — this is an async event,
            # so poll the predicate (recent_save_for returning None)
            # instead of sleeping for a fixed duration.
            wait_until(
                lambda: recent_save_for("/tmp/test.db") is None,
                timeout=2.0,
                interval=0.01,
                message="recent-save TTL did not expire",
            )
            result = recent_save_for("/tmp/test.db")
            self.assertIsNone(result)
        finally:
            rsh.RECENT_SAVE_TTL_S = original_ttl

    def test_multiple_saves_same_path(self):
        note_saved("first", "/tmp/test.db")
        note_saved("second", "/tmp/test.db")
        result = recent_save_for("/tmp/test.db")
        self.assertEqual(result[0], "second")

    def test_clear_empties_state(self):
        note_saved("note-1", "/tmp/test.db")
        clear()
        result = recent_save_for("/tmp/test.db")
        self.assertIsNone(result)

    def test_oldest_evicted_on_new_save(self):
        original_ttl = RECENT_SAVE_TTL_S
        try:
            import recent_save_hint as rsh

            rsh.RECENT_SAVE_TTL_S = 0.05
            note_saved("stale", "/tmp/test.db")
            # Wait for the stale entry's TTL to expire (async event).
            wait_until(
                lambda: recent_save_for("/tmp/test.db") is None,
                timeout=2.0,
                interval=0.01,
                message="stale recent-save TTL did not expire",
            )
            note_saved("fresh", "/tmp/test.db")
            result = recent_save_for("/tmp/test.db")
            self.assertEqual(result[0], "fresh")
        finally:
            rsh.RECENT_SAVE_TTL_S = original_ttl

    def test_unknown_path(self):
        note_saved("note-1", "/tmp/db1.db")
        result = recent_save_for("/tmp/db2.db")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
