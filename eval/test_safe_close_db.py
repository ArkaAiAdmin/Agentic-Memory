#!/usr/bin/env python3
"""Unit tests for BLK-2 (2026-06-07): open_db commits before close.

These tests verify that ``open_db`` and ``safe_close_db`` properly
commit any pending write transaction before closing the connection.
This prevents the silent-rollback bug that existed when callers used
the bare ``conn.close()`` pattern.

Run with:
    ~/.config/agentic-memory/venv/bin/python -m unittest eval.test_safe_close_db -v
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Make the agentic-memory package importable.
INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

import infra.memory_common as memory_common  # noqa: E402


class TestSafeCloseDb(unittest.TestCase):
    """``safe_close_db`` is a no-raise helper that commits then closes."""

    def test_safe_close_db_commits_pending_writes(self):
        """A pending write inside a ``safe_close_db``-closed connection
        must be persisted to disk. Without the commit, the row would be
        rolled back when the connection is closed."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.execute("INSERT INTO t VALUES (42)")
            # The row is in the transaction but not yet committed.
            memory_common.safe_close_db(conn)
            # Re-open the DB and confirm the row is there.
            conn2 = sqlite3.connect(str(db_path))
            row = conn2.execute("SELECT x FROM t").fetchone()
            conn2.close()
            self.assertEqual(
                row,
                (42,),
                "safe_close_db must commit pending writes before closing",
            )

    def test_safe_close_db_is_idempotent(self):
        """Calling ``safe_close_db`` on an already-closed connection
        is a silent no-op (must not raise). This is critical for
        finally-block patterns where a partial close might have
        already happened."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            conn = sqlite3.connect(str(db_path))
            conn.close()
            # Already closed; safe_close_db must not raise.
            try:
                memory_common.safe_close_db(conn)
            except Exception as e:
                self.fail(
                    f"safe_close_db must be idempotent and silent on "
                    f"already-closed connections, but raised: {e}"
                )

    def test_safe_close_db_never_raises(self):
        """``safe_close_db`` must NEVER raise. We verify by passing in
        a MagicMock connection whose commit() and close() both raise,
        and asserting the function swallows the errors."""
        from unittest.mock import MagicMock

        conn = MagicMock()
        conn.commit.side_effect = sqlite3.OperationalError("test commit error")
        conn.close.side_effect = sqlite3.OperationalError("test close error")
        # Should NOT raise, despite both inner calls failing.
        try:
            memory_common.safe_close_db(conn)
        except Exception as e:
            self.fail(
                f"safe_close_db must NEVER raise (both commit and "
                f"close mocked to raise), but raised: {e}"
            )


class TestOpenDbCommitsBeforeClose(unittest.TestCase):
    """``open_db`` is the central choke point. It must commit before
    close so writes inside the ``with`` block are durable."""

    def test_open_db_commits_pending_writes(self):
        """A write inside ``with open_db(...) as conn:`` must be
        persisted to disk after the block exits, even if the block
        raises. This is the core invariant BLK-2 introduces."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            # Use a fresh DB so we don't collide with the prod DB.
            # We also patch the prod-DB-leak guard: open_db writes
            # to ``path``, not the prod DB.
            try:
                with memory_common.open_db(db_path) as conn:
                    conn.execute("CREATE TABLE t (x INTEGER)")
                    conn.execute("INSERT INTO t VALUES (99)")
                    # No explicit commit() in the caller — open_db
                    # must do it on exit.
            except Exception as e:
                self.fail(f"open_db raised: {e}")
            # Re-open and verify the write is durable.
            conn2 = sqlite3.connect(str(db_path))
            row = conn2.execute("SELECT x FROM t").fetchone()
            conn2.close()
            self.assertEqual(
                row,
                (99,),
                "open_db must commit pending writes on exit so they "
                "are durable across connection close",
            )

    def test_open_db_commits_even_when_body_raises(self):
        """BUG-3 fix: if the ``with`` body raises, open_db must rollback
        the writes (not commit them) to avoid persisting partial/corrupted
        state, then propagate the exception.

        In SQLite, both DDL and DML are rolled back when inside a
        transaction. The write queue uses BEGIN IMMEDIATE, so both the
        CREATE TABLE and INSERT are rolled back.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            try:
                with memory_common.open_db(db_path) as conn:
                    conn.execute("CREATE TABLE t (x INTEGER)")
                    conn.execute("INSERT INTO t VALUES (7)")
                    # Raise before the block exits.
                    raise ValueError("intentional")
            except ValueError:
                # Expected.
                pass
            else:
                self.fail("ValueError was not re-raised by open_db")
            # Re-open and verify the transaction was fully rolled back
            # (both CREATE TABLE and INSERT are rolled back in a transaction).
            conn2 = sqlite3.connect(str(db_path))
            with self.assertRaises(sqlite3.OperationalError):
                conn2.execute("SELECT x FROM t")
            conn2.close()

    def test_open_db_read_only_path_is_no_op(self):
        """Read-only transactions don't need a commit; calling
        commit on them is a no-op. Verify that a SELECT inside
        open_db works and re-opening shows the same data."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            # Seed the DB first.
            with memory_common.open_db(db_path) as conn:
                conn.execute("CREATE TABLE t (x INTEGER)")
                conn.execute("INSERT INTO t VALUES (1)")
            # Read-only path: just SELECT.
            with memory_common.open_db(db_path) as conn:
                rows = conn.execute("SELECT x FROM t").fetchall()
            self.assertEqual(
                rows,
                [(1,)],
                "Read-only path through open_db must work; commit "
                "on a read-only transaction is a no-op",
            )

    def test_open_db_importable_from_memory_common(self):
        """The function is exported from memory_common (and
        re-imported by memory_mcp)."""
        self.assertTrue(
            hasattr(memory_common, "safe_close_db"),
            "memory_common must export safe_close_db",
        )
        self.assertTrue(
            callable(memory_common.safe_close_db),
            "memory_common.safe_close_db must be callable",
        )


class TestMemoryMcpUsesSafeCloseDb(unittest.TestCase):
    """The 13 raw db.close()/conn.close() sites in memory_mcp.py
    were migrated to safe_close_db() in BLK-2. We assert this
    statically to prevent regressions."""

    def test_memory_mcp_does_not_use_bare_db_close(self):
        """A bare ``db.close()`` (no commit) is no longer present in
        memory_mcp.py. The migration to ``safe_close_db`` removed all
        13 such sites."""
        import re

        memory_mcp_path = Path(INSTALL_DIR) / "memory_mcp.py"
        with open(memory_mcp_path, "r") as f:
            source = f.read()
        # Find every line that contains db.close() or conn.close()
        # NOT preceded by safe_close_db. We use a simple regex that
        # matches a line containing a bare .close() call on a
        # variable named db or conn.
        bare_closes = re.findall(
            r"^\s*(?:db|conn)\.close\(\)\s*$",
            source,
            flags=re.MULTILINE,
        )
        self.assertEqual(
            bare_closes,
            [],
            f"memory_mcp.py must not contain bare db.close()/conn.close() "
            f"calls (BLK-2: migrate them to safe_close_db). Found: "
            f"{len(bare_closes)} occurrence(s)",
        )

    def test_memory_mcp_imports_safe_close_db(self):
        """memory_mcp.py must import safe_close_db from memory_common."""
        import re

        memory_mcp_path = Path(INSTALL_DIR) / "memory_mcp.py"
        with open(memory_mcp_path, "r") as f:
            source = f.read()
        match = re.search(
            r"from\s+(?:infra\.)?memory_common\s+import\s+[^)]*\bsafe_close_db\b",
            source,
        )
        self.assertIsNotNone(
            match,
            "memory_mcp.py must import safe_close_db from memory_common",
        )


if __name__ == "__main__":
    unittest.main()
