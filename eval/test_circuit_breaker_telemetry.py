#!/usr/bin/env python3
"""Circuit-breaker telemetry tests.

Audit-gap fix (2026-06-22 follow-up): the auto-save circuit breaker
state was in-memory only.  ``_persist_circuit_state`` now writes
``auto_save_circuit_open`` and ``auto_save_circuit_close`` events to
``memory_audit_log`` so operators can see the open/close history
across process restarts.  ``memory_circuit_breaker_status`` is the
admin tool that surfaces them.

Coverage:
    1. _persist_circuit_state writes to memory_audit_log.
    2. open event recorded on breaker transition.
    3. close event recorded when state recovers.
    4. close event NOT recorded if breaker was never open.
    5. memory_circuit_breaker_status returns events newest first.
    6. memory_circuit_breaker_status respects limit.
    7. memory_circuit_breaker_status respects since_ts.
    8. memory_circuit_breaker_status returns empty on clean DB.
    9. Persistence failure is non-fatal (logs, doesn't raise).
   10. Invalid limit rejected.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from _lazy_imports import open_db  # noqa: E402


class _CbTestBase(unittest.TestCase):
    """Shared setup: fresh temp DB, breaker state reset, MEMORY_DB_PATH patched."""

    def setUp(self) -> None:
        from auto_save import _AUTO_SAVE_STATE, _AUTO_SAVE_STATE_LOCK

        with _AUTO_SAVE_STATE_LOCK:
            _AUTO_SAVE_STATE["failure_times"] = []
            _AUTO_SAVE_STATE["circuit_open_until"] = 0.0
            _AUTO_SAVE_STATE["last_backoff_seconds"] = 0.0

        self.tmp = Path(tempfile.mkdtemp(prefix="cb_telem_"))
        self.db_path = self.tmp / "memory.db"
        # Ensure the directory exists and migrations run.
        with open_db(self.db_path) as conn:
            pass

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    @contextmanager
    def temp_db(self):
        """Patch MEMORY_DB_PATH so all auto_save + mcp_audit reads
        point at self.db_path.

        ``_resolve_memory_dir`` (mcp_audit) honours MEMORY_DB_PATH
        directly.  ``get_db_path`` (auto_save) honours it too.  So a
        single env-var patch routes both to the temp DB.
        """
        with mock.patch.dict(os.environ, {"MEMORY_DB_PATH": str(self.db_path)}):
            yield

    def _audit_rows(self, where: str = "1", params: tuple = ()) -> list[dict]:
        with open_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM memory_audit_log WHERE {where} ORDER BY id DESC",
                params,
            ).fetchall()
        return [dict(r) for r in rows]


class TestPersistCircuitState(_CbTestBase):
    """_persist_circuit_state writes to memory_audit_log."""

    def test_writes_to_audit_log(self) -> None:
        from auto_save import _persist_circuit_state

        with self.temp_db():
            _persist_circuit_state(
                "open",
                details={
                    "n_failures": 5,
                    "window_s": 60.0,
                    "cb_seconds": 300.0,
                    "open_until": 999.0,
                },
            )

        rows = self._audit_rows("tool = ?", ("auto_save_circuit_open",))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool"], "auto_save_circuit_open")
        args = json.loads(rows[0]["args"])
        self.assertEqual(args["n_failures"], 5)
        self.assertEqual(args["window_s"], 60.0)
        self.assertEqual(args["cb_seconds"], 300.0)
        self.assertEqual(args["open_until"], 999.0)

    def test_persistence_failure_is_non_fatal(self) -> None:
        """If the audit log is unavailable, _persist_circuit_state logs
        and swallows the error rather than raising."""
        from auto_save import _persist_circuit_state

        # Point MEMORY_DB_PATH at a non-existent directory so the
        # conn lookup fails.
        bad = self.tmp / "nonexistent" / "memory.db"
        with mock.patch.dict(os.environ, {"MEMORY_DB_PATH": str(bad)}):
            # Should not raise.
            _persist_circuit_state("open", details={"n_failures": 1})


class TestBreakerTransitions(_CbTestBase):
    """The breaker persists open/close events at the right moments."""

    def test_open_event_recorded_on_breaker_trip(self) -> None:
        """When failure count exceeds max_retries, an open event is persisted.

        The config is a frozen dataclass so we can't mock.patch on
        it directly.  Instead, pre-seed the failure window so the
        next call trips the breaker under the default threshold.
        """
        from auto_save import (
            _auto_save_record_failure_and_maybe_trip,
            _AUTO_SAVE_STATE,
            _AUTO_SAVE_STATE_LOCK,
        )
        import time as _t

        # The default auto_save_max_retries is 3.  Pre-load 4
        # timestamps within the default 60s window so the next call
        # exceeds the threshold and trips the breaker.
        now = _t.time()
        with _AUTO_SAVE_STATE_LOCK:
            _AUTO_SAVE_STATE["failure_times"] = [now - 1, now - 1, now - 1, now - 1]
            _AUTO_SAVE_STATE["circuit_open_until"] = 0.0

        with self.temp_db():
            _auto_save_record_failure_and_maybe_trip()

        rows = self._audit_rows("tool = ?", ("auto_save_circuit_open",))
        self.assertEqual(len(rows), 1)
        args = json.loads(rows[0]["args"])
        self.assertGreaterEqual(args["n_failures"], 5)  # 4 + this failure

    def test_close_event_recorded_on_recovery(self) -> None:
        from auto_save import (
            _auto_save_record_success,
            _AUTO_SAVE_STATE,
            _AUTO_SAVE_STATE_LOCK,
        )

        # Pre-populate the breaker state to simulate an open breaker.
        with _AUTO_SAVE_STATE_LOCK:
            _AUTO_SAVE_STATE["circuit_open_until"] = 999999999.0  # far future
        with self.temp_db():
            _auto_save_record_success()

        rows = self._audit_rows("tool = ?", ("auto_save_circuit_close",))
        self.assertEqual(len(rows), 1)

    def test_close_event_not_recorded_on_normal_success(self) -> None:
        """If the breaker was never open, a success should not log a close."""
        from auto_save import _auto_save_record_success

        # Breaker is closed (default state).
        with self.temp_db():
            _auto_save_record_success()

        rows = self._audit_rows("tool = ?", ("auto_save_circuit_close",))
        self.assertEqual(len(rows), 0)


class TestMemoryCircuitBreakerStatus(_CbTestBase):
    """memory_circuit_breaker_status admin tool."""

    def test_empty_db_returns_empty_list(self) -> None:
        from mcp_audit import memory_circuit_breaker_status

        with self.temp_db():
            result = json.loads(memory_circuit_breaker_status())
        self.assertTrue(result["ok"])
        self.assertEqual(result["events"], [])
        self.assertEqual(result["summary"]["total_events"], 0)
        self.assertEqual(result["summary"]["open_count"], 0)
        self.assertEqual(result["summary"]["close_count"], 0)

    def test_returns_events_newest_first(self) -> None:
        from mcp_audit import memory_circuit_breaker_status
        from auto_save import _persist_circuit_state

        with self.temp_db():
            _persist_circuit_state(
                "open", details={"n_failures": 1, "ts_marker": "first"}
            )
            _persist_circuit_state("close", details={"ts_marker": "second"})
            _persist_circuit_state(
                "open", details={"n_failures": 2, "ts_marker": "third"}
            )

            result = json.loads(memory_circuit_breaker_status(limit=10))

        self.assertTrue(result["ok"])
        events = result["events"]
        self.assertEqual(len(events), 3)
        # Newest first: the third write should be events[0].
        self.assertEqual(events[0]["args"]["ts_marker"], "third")
        self.assertEqual(events[2]["args"]["ts_marker"], "first")
        self.assertEqual(result["summary"]["open_count"], 2)
        self.assertEqual(result["summary"]["close_count"], 1)

    def test_respects_limit(self) -> None:
        from mcp_audit import memory_circuit_breaker_status
        from auto_save import _persist_circuit_state

        with self.temp_db():
            for i in range(5):
                _persist_circuit_state(
                    "open", details={"n_failures": i, "ts_marker": f"event_{i}"}
                )

            result = json.loads(memory_circuit_breaker_status(limit=2))

        events = result["events"]
        self.assertEqual(len(events), 2)
        self.assertEqual(result["summary"]["limit"], 2)
        # Total count should still report all 5, even though only 2 returned.
        self.assertEqual(result["summary"]["total_events"], 5)

    def test_respects_since_ts(self) -> None:
        import time
        from mcp_audit import memory_circuit_breaker_status
        from auto_save import _persist_circuit_state

        # Insert two events with a known time gap.
        with self.temp_db():
            _persist_circuit_state("open", details={"ts_marker": "old"})
        cutoff = time.time()
        # Sleep briefly so the next event has a later ts.
        time.sleep(0.05)
        with self.temp_db():
            _persist_circuit_state("open", details={"ts_marker": "new"})

        with self.temp_db():
            result = json.loads(memory_circuit_breaker_status(since_ts=cutoff))

        # Only the "new" event should be returned.
        markers = [e["args"]["ts_marker"] for e in result["events"]]
        self.assertEqual(markers, ["new"])

    def test_only_returns_circuit_breaker_events(self) -> None:
        """The status tool should not surface unrelated audit log entries."""
        from mcp_audit import memory_circuit_breaker_status
        from auto_save import _persist_circuit_state

        with self.temp_db():
            _persist_circuit_state("open", details={"n_failures": 1})

        # Add a non-circuit event to the audit log.
        with open_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO memory_audit_log (ts, tool, latency_ms) VALUES (?, ?, ?)",
                (0.0, "memory_search", 1.0),
            )
            conn.commit()

        with self.temp_db():
            result = json.loads(memory_circuit_breaker_status())

        tools = [e["tool"] for e in result["events"]]
        self.assertNotIn("memory_search", tools)
        self.assertIn("auto_save_circuit_open", tools)

    def test_invalid_limit_rejected(self) -> None:
        from mcp_audit import memory_circuit_breaker_status
        from mcp_common import ErrorCode

        with self.temp_db():
            result = memory_circuit_breaker_status(limit=0)
        # _err returns a structured envelope, not a JSON object.
        self.assertIn(ErrorCode.INVALID_PARAMS.value, result)
        self.assertIn("limit must be 1..200", result)

        with self.temp_db():
            result = memory_circuit_breaker_status(limit=1000)
        self.assertIn(ErrorCode.INVALID_PARAMS.value, result)


if __name__ == "__main__":
    unittest.main()
