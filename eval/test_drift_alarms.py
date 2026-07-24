"""Unit tests for the v15 drift_alarms table + memory_list_drift_alarms tool.

Tests:
  * drift_alarms table schema (via migration_runner on a fresh DB)
  * check_concept_drift_db writes to drift_alarms when drift >= threshold
  * memory_list_drift_alarms returns rows, filters by acknowledged/alarm_level
  * memory_list_drift_alarms acknowledge flow updates rows atomically
  * memory_list_drift_alarms returns total_unacknowledged for dashboards

Uses a temp DB to avoid polluting the live one. Each test runs the
migration setup to ensure the drift_alarms table exists.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.chdir("..")
sys.path.insert(0, os.getcwd())


class TestDriftAlarmsSchema(unittest.TestCase):
    """Verify the migration creates the expected drift_alarms table."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="drift_alarms_"))
        self.db_path = self.tmpdir / "memory.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        # Bring the DB up to current schema (memories + all migrations)
        from infra.db_migrations import run_schema_setup

        run_schema_setup(conn)
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_drift_alarms_table_exists(self):
        conn = sqlite3.connect(str(self.db_path))
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='drift_alarms'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row, "drift_alarms table not created by migration")

    def test_drift_alarms_schema_has_required_columns(self):
        conn = sqlite3.connect(str(self.db_path))
        cols = [
            r[1] for r in conn.execute("PRAGMA table_info(drift_alarms)").fetchall()
        ]
        conn.close()
        for required in (
            "id",
            "memory_id",
            "concept",
            "drift_score",
            "threshold",
            "alarm_level",
            "detected_at",
            "acknowledged_at",
            "acknowledged_by",
            "notes",
        ):
            self.assertIn(required, cols, f"drift_alarms missing column {required!r}")

    def test_drift_alarms_check_constraint(self):
        conn = sqlite3.connect(str(self.db_path))
        # Insert a fake memory first (FK requirement)
        conn.execute(
            "INSERT INTO memories (id, content, source_file, created_at, updated_at, observed_at) "
            "VALUES ('test/mem1', 'test content', 'test', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        # Valid alarm_level passes
        conn.execute(
            "INSERT INTO drift_alarms (memory_id, concept, drift_score, threshold, alarm_level, detected_at) "
            "VALUES ('test/mem1', 'test_concept', 0.5, 0.15, 'info', '2026-06-22T00:00:00Z')"
        )
        conn.commit()
        # Invalid alarm_level fails
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO drift_alarms (memory_id, concept, drift_score, threshold, alarm_level, detected_at) "
                "VALUES ('test/mem1', 'bad', 0.5, 0.15, 'INVALID', '2026-06-22T00:00:00Z')"
            )
        conn.close()

    def test_drift_alarms_indexes_exist(self):
        conn = sqlite3.connect(str(self.db_path))
        idx_names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='drift_alarms'"
            ).fetchall()
        ]
        conn.close()
        for required in (
            "idx_drift_alarms_memory",
            "idx_drift_alarms_detected",
            "idx_drift_alarms_unack",
        ):
            self.assertIn(required, idx_names, f"index {required!r} missing")


class TestMemoryListDriftAlarms(unittest.TestCase):
    """Test the memory_list_drift_alarms MCP tool against a temp DB."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="drift_alarms_tool_"))
        self.db_path = self.tmpdir / "memory.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        from infra.db_migrations import run_schema_setup

        run_schema_setup(conn)
        conn.commit()
        conn.close()
        # Add a couple of memories
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO memories (id, content, source_file, created_at, updated_at, observed_at) "
            "VALUES ('lessons/test1', 'c1', 'f', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO memories (id, content, source_file, created_at, updated_at, observed_at) "
            "VALUES ('lessons/test2', 'c2', 'f', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO drift_alarms (memory_id, concept, drift_score, threshold, alarm_level, detected_at) "
            "VALUES ('lessons/test1', 'embedding_dim_top4', 0.20, 0.15, 'info', '2026-06-22T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO drift_alarms (memory_id, concept, drift_score, threshold, alarm_level, detected_at) "
            "VALUES ('lessons/test2', 'embedding_dim_top7', 0.50, 0.15, 'critical', '2026-06-22T01:00:00Z')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_list_unacknowledged(self):
        from mcp_surface.mcp_ctr_drift import memory_list_drift_alarms

        with patch("mcp_surface.mcp_ctr_drift._resolve_memory_dir", return_value=self.tmpdir):
            result = json.loads(memory_list_drift_alarms(acknowledged=False, limit=10))
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["total_unacknowledged"], 2)
        self.assertEqual(result["acknowledged_now"], 0)

    def test_list_by_level(self):
        from mcp_surface.mcp_ctr_drift import memory_list_drift_alarms

        with patch("mcp_surface.mcp_ctr_drift._resolve_memory_dir", return_value=self.tmpdir):
            result = json.loads(
                memory_list_drift_alarms(alarm_level="critical", limit=10)
            )
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["alarms"][0]["alarm_level"], "critical")

    def test_acknowledge_flow(self):
        from mcp_surface.mcp_ctr_drift import memory_list_drift_alarms

        # Get IDs of unack alarms
        with patch("mcp_surface.mcp_ctr_drift._resolve_memory_dir", return_value=self.tmpdir):
            r1 = json.loads(memory_list_drift_alarms(acknowledged=False, limit=10))
            ids = [a["id"] for a in r1["alarms"]]
            # Acknowledge all
            r2 = json.loads(
                memory_list_drift_alarms(
                    acknowledge_ids=ids, acknowledged_by="tester", notes="checked"
                )
            )
        self.assertEqual(r2["acknowledged_now"], len(ids))
        self.assertEqual(r2["total_unacknowledged"], 0)
        # Now all alarms show acknowledged_at set
        for a in r2["alarms"]:
            self.assertIsNotNone(a["acknowledged_at"])
            self.assertEqual(a["acknowledged_by"], "tester")
            self.assertEqual(a["notes"], "checked")

    def test_acknowledge_is_idempotent(self):
        from mcp_surface.mcp_ctr_drift import memory_list_drift_alarms

        with patch("mcp_surface.mcp_ctr_drift._resolve_memory_dir", return_value=self.tmpdir):
            r1 = json.loads(memory_list_drift_alarms(acknowledged=False, limit=10))
            ids = [a["id"] for a in r1["alarms"]]
            # First ack
            r2 = json.loads(
                memory_list_drift_alarms(acknowledge_ids=ids, acknowledged_by="t1")
            )
            self.assertEqual(r2["acknowledged_now"], len(ids))
            # Second ack of same IDs — should ack 0 (all already acked)
            r3 = json.loads(
                memory_list_drift_alarms(acknowledge_ids=ids, acknowledged_by="t2")
            )
            self.assertEqual(r3["acknowledged_now"], 0)

    def test_invalid_alarm_level(self):
        from mcp_surface.mcp_ctr_drift import memory_list_drift_alarms

        with patch("mcp_surface.mcp_ctr_drift._resolve_memory_dir", return_value=self.tmpdir):
            result = memory_list_drift_alarms(alarm_level="bogus")
        self.assertTrue(result.startswith("Error [INVALID_PARAMS]"))

    def test_invalid_limit(self):
        from mcp_surface.mcp_ctr_drift import memory_list_drift_alarms

        with patch("mcp_surface.mcp_ctr_drift._resolve_memory_dir", return_value=self.tmpdir):
            result = memory_list_drift_alarms(limit=0)
        self.assertTrue(result.startswith("Error [INVALID_PARAMS]"))
        with patch("mcp_surface.mcp_ctr_drift._resolve_memory_dir", return_value=self.tmpdir):
            result = memory_list_drift_alarms(limit=1000)
        self.assertTrue(result.startswith("Error [INVALID_PARAMS]"))


class TestCheckConceptDriftWritePath(unittest.TestCase):
    """E7 fix (2026-06-22): tests for the *write* path.

    The tests in TestMemoryListDriftAlarms cover the *read* path
    (``memory_list_drift_alarms``).  But the production code that
    populates the ``drift_alarms`` table is
    ``search.orchestrator.check_concept_drift_db`` (the MCP tool
    handler for ``memory_check_concept_drift``), via
    ``_record_drift_event``.  This test exercises the real
    write path end-to-end, not a mock.

    Coverage:
      * ``check_concept_drift_db`` writes a row to ``concept_drift``.
      * ``check_concept_drift_db`` writes per-memory rows to
        ``drift_alarms`` (capped at 10, but with one memory we
        should see exactly 1 alarm).
      * Calling ``check_concept_drift_db`` twice within
        ``min_seconds_between_writes`` (G8) is a no-op the second
        time.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="drift_alarms_write_"))
        self.db_path = self.tmpdir / "memory.db"
        # Bring the DB up to current schema.
        from infra.db_migrations import run_schema_setup

        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        run_schema_setup(conn)
        conn.commit()
        conn.close()

        # Insert a memory + an embedding so the centroid computation
        # has at least one vector to work with.  We use a small
        # 4-dim vector; the centroid is a no-op for 1 row but
        # ``_record_drift_event`` is still exercised end-to-end.
        import struct

        vec = struct.pack("4f", 0.1, 0.2, 0.3, 0.4)
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO memories (id, content, source_file, created_at, updated_at, observed_at) "
            "VALUES ('lessons/drift-write', 'c', 'f', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO memory_embeddings "
            "(memory_id, content_hash, embedding, model_revision, dim, updated_at) "
            "VALUES ('lessons/drift-write', 'h', ?, 'test-model', 4, 0.0)",
            (vec,),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_check_concept_drift_writes_baseline(self):
        """First call writes a baseline row (is_baseline=True)."""
        from search.orchestrator import check_concept_drift_db

        result = check_concept_drift_db(self.db_path, threshold=0.15)
        # On a fresh DB with no prior centroid, drift==0 and
        # is_baseline=True, so the writer should still record a
        # baseline row.
        self.assertIn("alarm_id", result)
        # Confirm the row landed in concept_drift.
        conn = sqlite3.connect(str(self.db_path))
        n = conn.execute("SELECT COUNT(*) FROM concept_drift").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1, "expected one concept_drift row from baseline")

    def test_check_concept_drift_writes_drift_alarm(self):
        """Baseline write should also produce at least 1 drift_alarm row."""
        from search.orchestrator import check_concept_drift_db

        check_concept_drift_db(self.db_path, threshold=0.15)
        conn = sqlite3.connect(str(self.db_path))
        n = conn.execute(
            "SELECT COUNT(*) FROM drift_alarms WHERE memory_id = 'lessons/drift-write'"
        ).fetchone()[0]
        conn.close()
        self.assertGreaterEqual(
            n,
            1,
            "expected at least 1 drift_alarms row for the only embedded memory",
        )

    def test_check_concept_drift_dedupes_within_window(self):
        """G8 fix: a second call within min_seconds_between_writes
        must NOT write a duplicate concept_drift row."""
        from search.orchestrator import check_concept_drift_db

        # First call writes the baseline.
        check_concept_drift_db(self.db_path, threshold=0.15)
        # Second call within 60s of the first should be a no-op.
        result2 = check_concept_drift_db(self.db_path, threshold=0.15)
        self.assertEqual(
            result2.get("alarm_id", ""),
            "",
            "second call within dedupe window should not write a new alarm",
        )
        conn = sqlite3.connect(str(self.db_path))
        n = conn.execute("SELECT COUNT(*) FROM concept_drift").fetchone()[0]
        conn.close()
        self.assertEqual(
            n,
            1,
            "expected exactly 1 concept_drift row after back-to-back calls "
            "(G8 dedupe window)",
        )


if __name__ == "__main__":
    unittest.main()
