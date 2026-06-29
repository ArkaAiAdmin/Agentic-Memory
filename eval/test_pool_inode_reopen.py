#!/usr/bin/env python3
"""Regression tests for the connection pool's inode-tracking reopen logic.

Background:
  2026-06-26: long-running daemons (auto_save.py, memory_mcp.py) held
  sqlite connections to old inodes after rebuild_index.py os.replace()'d
  memory.db. The connections were still valid (the old inode was kept
  alive by the open fd) but stale — writes through them never reached
  the new file. The fix: track the inode each connection was opened
  against, and on get() compare against the current st_ino. If they
  differ, close the old conn and reopen against the new file.

These tests cover:
  - Inode is recorded when a new conn is created
  - Inode mismatch is detected and the old conn is evicted
  - Inode tracking is cleared on close() and close_all()
  - No eviction when inode is unchanged
  - No eviction when inode can't be stat'd (graceful degradation)
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from db import _ConnectionPool  # noqa: E402


def _make_db(path: Path) -> None:
    """Create a minimal sqlite db at *path* with a memories table."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memories ("
        "id TEXT PRIMARY KEY, content TEXT, value INTEGER)"
    )
    conn.execute("INSERT INTO memories VALUES ('a', 'first', 1)")
    conn.commit()
    conn.close()


class TestPoolInodeTracking(unittest.TestCase):
    """The pool records and reopens on inode changes."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"
        _make_db(self.db_path)
        self.pool = _ConnectionPool()

    def tearDown(self):
        self.pool.close_all()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_inode_recorded_on_new_conn(self):
        """get() records the current inode of the db file."""
        import time as _time

        _time.sleep(0.01)  # ensure inode is stable
        conn = self.pool.get(str(self.db_path), timeout=5.0)
        try:
            key = self._key_for(str(self.db_path))
            self.assertIn(key, self.pool._inodes)
            expected_ino = os.stat(self.db_path).st_ino
            self.assertEqual(self.pool._inodes[key], expected_ino)
        finally:
            self.pool.put(conn)

    def test_inode_mismatch_evicts_conn(self):
        """get() detects inode change and reopens against new file."""

        # Open initial conn
        conn1 = self.pool.get(str(self.db_path), timeout=5.0)
        try:
            initial_ino = os.stat(self.db_path).st_ino
            self.assertIn(self._key_for(str(self.db_path)), self.pool._inodes)
        finally:
            self.pool.put(conn1)

        # Simulate a replacement: write to a new file then rename
        # over the existing path. This is what rebuild_index.py does
        # (os.replace(tmp_db_path, db_path)).
        new_path = self.tmpdir / "new.db"
        _make_db(new_path)
        # The new file gets a different inode than the old one
        new_ino = os.stat(new_path).st_ino
        self.assertNotEqual(new_ino, initial_ino, "test setup: inodes should differ")

        os.replace(str(new_path), str(self.db_path))

        # The next get() should detect the mismatch, close the old
        # conn, and open a fresh one against the new inode.
        conn2 = self.pool.get(str(self.db_path), timeout=5.0)
        try:
            current_ino = os.stat(self.db_path).st_ino
            self.assertEqual(current_ino, new_ino)
            # The stored inode should now be the new one
            self.assertEqual(
                self.pool._inodes[self._key_for(str(self.db_path))], new_ino
            )
        finally:
            self.pool.put(conn2)

    def test_inode_unchanged_no_eviction(self):
        """get() does not evict the conn when inode is unchanged."""
        conn1 = self.pool.get(str(self.db_path), timeout=5.0)
        try:
            conn_id_1 = id(conn1)
        finally:
            self.pool.put(conn1)

        conn2 = self.pool.get(str(self.db_path), timeout=5.0)
        try:
            self.assertEqual(id(conn2), conn_id_1, "should reuse the same conn")
        finally:
            self.pool.put(conn2)

    def test_close_clears_inode(self):
        """close(path) evicts the stored inode."""
        self.pool.get(str(self.db_path), timeout=5.0)
        key = self._key_for(str(self.db_path))
        self.assertIn(key, self.pool._inodes)

        self.pool.close(str(self.db_path))
        self.assertNotIn(key, self.pool._inodes)

    def test_close_all_clears_inodes(self):
        """close_all() clears every stored inode."""
        self.pool.get(str(self.db_path), timeout=5.0)
        self.pool.close_all()
        self.assertEqual(self.pool._inodes, {})

    def test_inode_unavailable_graceful(self):
        """If os.stat fails, get() doesn't churn connections.

        Simulates a path that's been deleted between conn creation
        and the next get() — the inode can't be stat'd, so we leave
        the old conn in place rather than thrashing.
        """
        conn1 = self.pool.get(str(self.db_path), timeout=5.0)
        try:
            id(conn1)
        finally:
            self.pool.put(conn1)

        # Force _inode_of to return 0 by patching os.stat in the
        # pool's module. With the path deleted AND inode=0, no
        # mismatch is detected — the pool falls through and tries
        # the SELECT 1 probe instead, which is the existing
        # stale-conn check.
        with patch("db.os.stat", side_effect=OSError("no such file")):
            conn2 = self.pool.get(str(self.db_path), timeout=5.0)
        try:
            # Either the same conn (if SELECT 1 still works) or a
            # new one (if SELECT 1 also failed). Either way, no
            # crash, and we got a working connection back.
            cur = conn2.execute("SELECT 1").fetchone()
            self.assertEqual(cur[0], 1)
        finally:
            self.pool.put(conn2)

    def _key_for(self, path: str) -> tuple:
        """Get the pool's internal key for *path* on the current thread."""
        import threading

        return (path, threading.current_thread().ident or 0)


if __name__ == "__main__":
    unittest.main()
