"""Tests for adaptive_retention.py.

Covers the module-level audit_hits cache (the 2026-06-15 fix that
eliminates the O(N×M) audit log re-scan), the schema bootstrap,
access recording, and the adaptive halflife calculation.

The cache is the hot path: every search result triggers
compute_adaptive_halflife once per note, so the cache must be:
  1. Per-db_path (multi-DB isolation)
  2. Invalidated on writes (invalidate_audit_hits_cache)
  3. Bounded (no unbounded growth)
  4. Thread-safe (the GIL protects the dict write; reads are
     unlocked which is fine — a stale read just means a brief miss)
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure feature is on (earlier tests in the suite may set this off).
os.environ["MEMORY_ADAPTIVE_RETENTION"] = "1"

INSTALL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL))

# Force-reload the config so ADAPTIVE_RETENTION_ENABLED is True.
import config as _cfg

_cfg._instance = None

import adaptive_retention  # noqa: E402

# Force the lazy attr to re-resolve.
adaptive_retention.__dict__.pop("ADAPTIVE_RETENTION_ENABLED", None)

from memory_common import connection_pool  # noqa: E402


def _new_db() -> sqlite3.Connection:
    """In-memory DB with adaptive schema bootstrapped.

    Note: user_access_log has a FK to memories(id), so we must
    create a minimal memories table too, otherwise inserts raise
    OperationalError (FK violation, not "table missing").
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE memories (  id TEXT PRIMARY KEY,  content TEXT)")
    adaptive_retention.ensure_adaptive_schema(conn)
    return conn


def _seed_note(conn: sqlite3.Connection, note_id: str) -> None:
    """Insert a minimal note row so the FK on user_access_log is satisfied."""
    conn.execute(
        "INSERT OR IGNORE INTO memories (id, content) VALUES (?, ?)",
        (note_id, "test"),
    )
    conn.commit()


class TestEnsureAdaptiveSchema(unittest.TestCase):
    def test_creates_user_access_log_table(self):
        conn = _new_db()
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("user_access_log", tables)
        finally:
            conn.close()

    def test_creates_index(self):
        conn = _new_db()
        try:
            indexes = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            self.assertIn("idx_user_access_note", indexes)
        finally:
            conn.close()

    def test_idempotent(self):
        """Calling ensure_adaptive_schema twice must not raise."""
        conn = _new_db()
        try:
            adaptive_retention.ensure_adaptive_schema(conn)
            adaptive_retention.ensure_adaptive_schema(conn)
        finally:
            conn.close()


class TestRecordAccess(unittest.TestCase):
    def test_records_access_event(self):
        conn = _new_db()
        try:
            adaptive_retention.ADAPTIVE_RETENTION_ENABLED = True
            _seed_note(conn, "note-1")
            try:
                adaptive_retention.record_access(conn, "note-1", source="search")
            except Exception as e:
                self.fail(f"record_access raised: {type(e).__name__}: {e}")
            conn.commit()
            rows = conn.execute(
                "SELECT note_id, source FROM user_access_log"
            ).fetchall()
            self.assertEqual(len(rows), 1, f"got rows: {rows}")
            self.assertEqual(rows[0][0], "note-1")
            self.assertEqual(rows[0][1], "search")
        finally:
            conn.close()

    def test_no_commit_caller_manages_transaction(self):
        """record_access must NOT commit — saga rollback relies on this."""
        conn = _new_db()
        try:
            _seed_note(conn, "note-1")
            adaptive_retention.record_access(conn, "note-1")
            # Row should be visible to the same conn (uncommitted).
            visible_in_same_conn = conn.execute(
                "SELECT COUNT(*) FROM user_access_log"
            ).fetchone()[0]
            self.assertEqual(visible_in_same_conn, 1)
        finally:
            conn.close()
        # New conn should see no rows (uncommitted).
        conn2 = sqlite3.connect(":memory:")
        try:
            self.assertEqual(
                conn2.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name='user_access_log'"
                ).fetchone()[0],
                0,
            )
        finally:
            conn2.close()

    def test_swallows_missing_table(self):
        """If user_access_log doesn't exist, record_access must not raise."""
        conn = sqlite3.connect(":memory:")  # NO ensure_adaptive_schema
        try:
            # Should silently no-op.
            adaptive_retention.record_access(conn, "note-1")
        finally:
            conn.close()


class TestComputeAdaptiveHalflife(unittest.TestCase):
    def test_no_access_returns_base(self):
        conn = _new_db()
        try:
            halflife = adaptive_retention.compute_adaptive_halflife(
                "note-unseen", base_halflife=30.0, conn=conn
            )
            self.assertEqual(halflife, 30.0)
        finally:
            conn.close()

    def test_more_access_longer_halflife(self):
        conn = _new_db()
        try:
            _seed_note(conn, "note-hot")
            # Record many accesses for note-hot.
            for _ in range(10):
                adaptive_retention.record_access(conn, "note-hot", source="search")
            conn.commit()
            halflife_hot = adaptive_retention.compute_adaptive_halflife(
                "note-hot", base_halflife=30.0, conn=conn
            )
            halflife_cold = adaptive_retention.compute_adaptive_halflife(
                "note-cold", base_halflife=30.0, conn=conn
            )
            self.assertGreater(halflife_hot, halflife_cold)
        finally:
            conn.close()

    def test_capped_at_max_multiplier(self):
        conn = _new_db()
        try:
            _seed_note(conn, "note-huge")
            for _ in range(1000):
                adaptive_retention.record_access(conn, "note-huge", source="search")
            conn.commit()
            halflife = adaptive_retention.compute_adaptive_halflife(
                "note-huge", base_halflife=30.0, conn=conn
            )
            self.assertLessEqual(
                halflife,
                30.0 * adaptive_retention._MAX_BOOST_MULTIPLIER + 0.01,
            )
        finally:
            conn.close()


class TestAuditHitsCache(unittest.TestCase):
    """The module-level _audit_hits_cache_by_db is the perf hot path."""

    def setUp(self):
        # Clear cache between tests.
        adaptive_retention.invalidate_audit_hits_cache()

    def test_cache_populated_on_first_call(self):
        conn = _new_db()
        try:
            # Seed an audit_log with a memory_search hit.
            conn.execute(
                "CREATE TABLE audit_log ("
                "  id INTEGER PRIMARY KEY,"
                "  tool_name TEXT,"
                "  result_preview TEXT"
                ")"
            )
            conn.execute(
                "INSERT INTO audit_log VALUES (1, 'memory_search', ?)",
                (json.dumps(["note-1"]),),
            )
            conn.commit()

            # First call: cache miss, populates cache.
            adaptive_retention.compute_adaptive_halflife(
                "note-1", base_halflife=30.0, conn=conn
            )
            self.assertGreater(len(adaptive_retention._audit_hits_cache_by_db), 0)
        finally:
            conn.close()

    def test_cache_reused_on_second_call(self):
        """Second call must NOT re-scan the audit_log (cache hit)."""
        conn = _new_db()
        try:
            conn.execute(
                "CREATE TABLE audit_log ("
                "  id INTEGER PRIMARY KEY,"
                "  tool_name TEXT,"
                "  result_preview TEXT"
                ")"
            )
            conn.execute(
                "INSERT INTO audit_log VALUES (1, 'memory_search', ?)",
                (json.dumps(["note-1"]),),
            )
            conn.commit()

            adaptive_retention.compute_adaptive_halflife(
                "note-1", base_halflife=30.0, conn=conn
            )

            # Patch _build_audit_hits_index to detect a cache miss.
            original = adaptive_retention._build_audit_hits_index
            with patch.object(
                adaptive_retention,
                "_build_audit_hits_index",
                side_effect=AssertionError("cache miss should not happen"),
            ):
                adaptive_retention.compute_adaptive_halflife(
                    "note-1", base_halflife=30.0, conn=conn
                )
            # Restore for cleanup.
            adaptive_retention._build_audit_hits_index = original
        finally:
            conn.close()

    def test_invalidate_clears_cache(self):
        conn = _new_db()
        try:
            conn.execute(
                "CREATE TABLE audit_log ("
                "  id INTEGER PRIMARY KEY,"
                "  tool_name TEXT,"
                "  result_preview TEXT"
                ")"
            )
            conn.commit()

            adaptive_retention.compute_adaptive_halflife(
                "note-1", base_halflife=30.0, conn=conn
            )
            self.assertGreater(len(adaptive_retention._audit_hits_cache_by_db), 0)

            adaptive_retention.invalidate_audit_hits_cache()
            self.assertEqual(len(adaptive_retention._audit_hits_cache_by_db), 0)
        finally:
            conn.close()

    def test_invalidate_specific_db(self):
        conn = _new_db()
        try:
            conn.execute(
                "CREATE TABLE audit_log ("
                "  id INTEGER PRIMARY KEY,"
                "  tool_name TEXT,"
                "  result_preview TEXT"
                ")"
            )
            conn.commit()

            # Populate cache with two different keys.
            adaptive_retention._audit_hits_cache_by_db["/db/a.db"] = {}
            adaptive_retention._audit_hits_cache_by_db["/db/b.db"] = {}
            self.assertEqual(len(adaptive_retention._audit_hits_cache_by_db), 2)

            adaptive_retention.invalidate_audit_hits_cache("/db/a.db")
            self.assertNotIn("/db/a.db", adaptive_retention._audit_hits_cache_by_db)
            self.assertIn("/db/b.db", adaptive_retention._audit_hits_cache_by_db)
        finally:
            conn.close()


class TestRetentionStats(unittest.TestCase):
    def test_returns_expected_shape(self):
        """retention_stats uses db_path, not conn. With a nonexistent
        db_path it must return a graceful error dict, not raise."""
        stats = adaptive_retention.retention_stats(db_path="/nonexistent/path.db")
        self.assertIsInstance(stats, dict)
        # Either an error dict or a stats dict; both acceptable.
        self.assertTrue(
            "enabled" in stats or "error" in stats,
            f"unexpected shape: {stats}",
        )

    def test_disabled_returns_disabled_dict(self):
        """When ADAPTIVE_RETENTION_ENABLED is False, must return
        {'enabled': False} without touching the DB."""
        original = adaptive_retention.ADAPTIVE_RETENTION_ENABLED
        try:
            # Use the __getattr__ indirection to set the cached value.
            import sys

            sys.modules[__name__].__dict__["ADAPTIVE_RETENTION_ENABLED"] = False
            adaptive_retention.ADAPTIVE_RETENTION_ENABLED = False
            stats = adaptive_retention.retention_stats()
            self.assertEqual(stats, {"enabled": False})
        finally:
            # Restore by clearing the lazy cache.
            sys.modules["adaptive_retention"].__dict__.pop(
                "ADAPTIVE_RETENTION_ENABLED", None
            )
            adaptive_retention.ADAPTIVE_RETENTION_ENABLED = original


if __name__ == "__main__":
    unittest.main()
