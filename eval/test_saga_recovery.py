#!/usr/bin/env python3
"""Regression tests for saga durability, WAL intent/done logging, crash recovery, and step timeouts (C9, M21, M23, L6)."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from infra.db import open_db
from infra.saga import Saga, SagaStep, SagaMode, SagaError, recover_incomplete_sagas, ensure_saga_log_table


class TestSagaRecovery(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "memory.db"
        with open_db(self.db_path) as conn:
            ensure_saga_log_table(conn)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_saga_wal_logging(self):
        """Verify intent and done steps are recorded in saga_log."""
        with open_db(self.db_path) as conn:
            step1 = SagaStep(name="step_a", do=lambda: "ok_a", undo=lambda: None)
            step2 = SagaStep(name="step_b", do=lambda: "ok_b", undo=lambda: None)
            with Saga("test_wal", [step1, step2], conn=conn, mode=SagaMode.DEFERRED) as saga:
                pass

            rows = conn.execute("SELECT step_name, status FROM saga_log ORDER BY id").fetchall()
            self.assertGreaterEqual(len(rows), 4)
            # Expect intent then done for step_a and step_b
            statuses = [(r[0], r[1]) for r in rows]
            self.assertIn(("step_a", "intent"), statuses)
            self.assertIn(("step_a", "done"), statuses)
            self.assertIn(("step_b", "intent"), statuses)
            self.assertIn(("step_b", "done"), statuses)

    def test_step_timeout_triggers_rollback(self):
        """Verify step_timeout_s triggers timeout exception and executes undo."""
        undone = []

        def slow_do():
            time.sleep(0.5)
            return "too slow"

        def slow_undo():
            undone.append("slow")

        step_slow = SagaStep(name="slow_step", do=slow_do, undo=slow_undo)

        with open_db(self.db_path) as conn:
            with self.assertRaises(SagaError):
                with Saga("timeout_saga", [step_slow], conn=conn, mode=SagaMode.DEFERRED, step_timeout_s=0.1):
                    pass

        self.assertIn("slow", undone)

    def test_recover_incomplete_sagas(self):
        """Verify recover_incomplete_sagas finds sagas with intent without terminal status."""
        with open_db(self.db_path) as conn:
            now = time.time()
            conn.execute(
                "INSERT INTO saga_log (saga_id, saga_name, step_idx, step_name, status, ts) "
                "VALUES ('uncommitted_123', 'interrupted_saga', 0, 'write_db', 'intent', ?)",
                (now,),
            )
            conn.execute(
                "INSERT INTO saga_log (saga_id, saga_name, step_idx, step_name, status, ts) "
                "VALUES ('uncommitted_123', 'interrupted_saga', 0, 'write_db', 'done', ?)",
                (now + 0.1,),
            )
            conn.execute(
                "INSERT INTO saga_log (saga_id, saga_name, step_idx, step_name, status, ts) "
                "VALUES ('uncommitted_123', 'interrupted_saga', 1, 'write_file', 'intent', ?)",
                (now + 0.2,),
            )
            conn.commit()

        with open_db(self.db_path) as conn:
            recovered_count = recover_incomplete_sagas(conn)
            self.assertEqual(recovered_count, 1)

            # Check that intent rows were marked undone
            undone_rows = conn.execute(
                "SELECT status FROM saga_log WHERE saga_id = 'uncommitted_123' AND status = 'undone'"
            ).fetchall()
            self.assertGreaterEqual(len(undone_rows), 1)


if __name__ == "__main__":
    unittest.main()
