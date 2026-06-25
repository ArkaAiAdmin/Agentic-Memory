from _fixtures import bootstrap_temp_db_clean

#!/usr/bin/env python3
"""Unit tests for audit.py + memory_mcp audit integration (Sprint 4 / P0 #4).

Run with:
    ~/.config/agentic-memory/venv/bin/python -m unittest eval.test_audit_log
or:
    ~/.config/agentic-memory/venv/bin/python eval/test_audit_log.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

import audit  # noqa: E402
import memory_common  # noqa: E402


def _init_db(db_path: Path) -> None:
    """H21: bootstrap with full prod schema (no custom schema)."""
    bootstrap_temp_db_clean(db_path)


def _count_rows(db_path: Path) -> int:
    """Force-commit any pending writes, return row count."""
    with memory_common.open_db(db_path, timeout=5.0) as conn:
        with conn:
            return conn.execute("SELECT COUNT(*) FROM memory_audit_log").fetchone()[0]


class TestEnqueueAudit(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="audit_enqueue_")
        self.db_path = Path(self.tmpdir) / "memory.db"
        _init_db(self.db_path)

    def tearDown(self):
        audit.flush_audit(timeout=1.0)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_enqueue_creates_row(self):
        audit.enqueue_audit(
            db_path=str(self.db_path),
            tool="memory_search",
            args='{"query": "x"}',
            results_count=3,
            top1_id="mem/abc",
            latency_ms=1.5,
        )
        self.assertTrue(audit.flush_audit(timeout=2.0))
        self.assertEqual(_count_rows(self.db_path), 1)

    def test_enqueue_with_error(self):
        audit.enqueue_audit(
            db_path=str(self.db_path),
            tool="memory_save",
            args="{}",
            error="RuntimeError('boom')",
            latency_ms=0.5,
        )
        self.assertTrue(audit.flush_audit(timeout=2.0))
        with memory_common.open_db(self.db_path, timeout=5.0) as conn:
            row = conn.execute(
                "SELECT error, latency_ms FROM memory_audit_log"
            ).fetchone()
        self.assertEqual(row[0], "RuntimeError('boom')")
        self.assertAlmostEqual(row[1], 0.5, delta=0.01)

    def test_enqueue_many_rows(self):
        N = 100
        for i in range(N):
            audit.enqueue_audit(
                db_path=str(self.db_path),
                tool=f"tool_{i % 5}",
                args=f'{{"i": {i}}}',
                latency_ms=0.1 * i,
            )
        self.assertTrue(audit.flush_audit(timeout=3.0))
        self.assertEqual(_count_rows(self.db_path), N)


class TestAuditContextManager(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="audit_ctx_")
        self.db_path = Path(self.tmpdir) / "memory.db"
        _init_db(self.db_path)

    def tearDown(self):
        audit.flush_audit(timeout=1.0)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_success_records_row(self):
        with audit.audit(
            "memory_search", args={"q": "x"}, db_path=str(self.db_path)
        ) as ctx:
            ctx["results_count"] = 7
            ctx["top1_id"] = "mem/abc"
        audit.flush_audit(timeout=2.0)
        with memory_common.open_db(self.db_path, timeout=5.0) as conn:
            row = conn.execute(
                "SELECT tool, args, results_count, top1_id, error, latency_ms "
                "FROM memory_audit_log"
            ).fetchone()
        self.assertEqual(row[0], "memory_search")
        self.assertIn('"q": "x"', row[1])
        self.assertEqual(row[2], 7)
        self.assertEqual(row[3], "mem/abc")
        self.assertIsNone(row[4])
        self.assertGreaterEqual(row[5], 0.0)

    def test_exception_captured_and_reraised(self):
        with self.assertRaises(RuntimeError):
            with audit.audit("memory_save", args={"x": 1}, db_path=str(self.db_path)):
                raise RuntimeError("simulated failure")
        audit.flush_audit(timeout=2.0)
        with memory_common.open_db(self.db_path, timeout=5.0) as conn:
            row = conn.execute(
                "SELECT error, latency_ms FROM memory_audit_log"
            ).fetchone()
        self.assertIn("simulated failure", row[0])
        self.assertIn("RuntimeError", row[0])
        self.assertGreater(row[1], 0.0)

    def test_no_args_call(self):
        with audit.audit("memory_arc_stats", db_path=str(self.db_path)):
            pass
        audit.flush_audit(timeout=2.0)
        with memory_common.open_db(self.db_path, timeout=5.0) as conn:
            row = conn.execute("SELECT tool, args FROM memory_audit_log").fetchone()
        self.assertEqual(row[0], "memory_arc_stats")
        self.assertIsNone(row[1])


class TestAsyncFlush(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="audit_async_")
        self.db_path = Path(self.tmpdir) / "memory.db"
        _init_db(self.db_path)

    def tearDown(self):
        audit.flush_audit(timeout=1.0)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_pending_counter_drains_on_flush(self):
        audit.enqueue_audit(
            db_path=str(self.db_path),
            tool="t",
            args="{}",
            latency_ms=0.1,
        )
        self.assertGreater(audit.audit_queue_size(), 0)
        self.assertTrue(audit.flush_audit(timeout=2.0))
        self.assertEqual(audit.audit_queue_size(), 0)
        self.assertEqual(_count_rows(self.db_path), 1)

    def test_no_double_counting_on_flush(self):
        for _ in range(10):
            audit.enqueue_audit(
                db_path=str(self.db_path),
                tool="t",
                args="{}",
                latency_ms=0.1,
            )
        audit.flush_audit(timeout=2.0)
        audit.flush_audit(timeout=2.0)
        self.assertEqual(_count_rows(self.db_path), 10)

    def test_thread_routes_multiple_dbs(self):
        db_b = Path(self.tmpdir) / "b.db"
        _init_db(db_b)
        for i in range(5):
            audit.enqueue_audit(
                db_path=str(self.db_path),
                tool="t",
                args="{}",
                latency_ms=0.1,
            )
            audit.enqueue_audit(
                db_path=str(db_b),
                tool="t",
                args="{}",
                latency_ms=0.1,
            )
        audit.flush_audit(timeout=2.0)
        self.assertEqual(_count_rows(self.db_path), 5)
        self.assertEqual(_count_rows(db_b), 5)

    def test_enqueue_doesnt_block(self):
        N = 2000
        t0 = time.perf_counter()
        for i in range(N):
            audit.enqueue_audit(
                db_path=str(self.db_path),
                tool="t",
                args="{}",
                latency_ms=0.01,
            )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        per_call = elapsed_ms / N
        self.assertLess(per_call, 5.0, f"enqueue too slow: {per_call:.3f}ms/call")


class TestAuditQueryTool(unittest.TestCase):
    """Verify memory_audit_query behaves correctly with real audit data."""

    @classmethod
    def setUpClass(cls):
        import memory_mcp
        import mcp_tools

        cls.memory_mcp = memory_mcp
        cls.mcp_tools = mcp_tools

    def setUp(self):
        self._saved_memory_db_path = os.environ.get("MEMORY_DB_PATH")
        self.tmpdir = tempfile.mkdtemp(prefix="audit_query_")
        self.test_mem = Path(self.tmpdir) / "memory"
        self.test_mem.mkdir(parents=True)
        self.db_path = self.test_mem / "memory.db"
        _init_db(self.db_path)
        os.environ["MEMORY_DB_PATH"] = str(self.db_path)
        self._orig_global = self.memory_mcp.GLOBAL_MEM_DIR
        self._orig_resolve = self.memory_mcp.resolve_active_memory_dir
        self.memory_mcp.GLOBAL_MEM_DIR = self.test_mem
        self.memory_mcp.resolve_active_memory_dir = lambda **_: self.test_mem
        self.mcp_tools.resolve_active_memory_dir = lambda **_: self.test_mem
        self.mcp_tools.GLOBAL_MEM_DIR = self.test_mem

    def tearDown(self):
        audit.flush_audit(timeout=1.0)
        if self._saved_memory_db_path is not None:
            os.environ["MEMORY_DB_PATH"] = self._saved_memory_db_path
        else:
            os.environ.pop("MEMORY_DB_PATH", None)
        self.memory_mcp.GLOBAL_MEM_DIR = self._orig_global
        self.memory_mcp.resolve_active_memory_dir = self._orig_resolve
        self.mcp_tools.resolve_active_memory_dir = self._orig_resolve
        self.mcp_tools.GLOBAL_MEM_DIR = self._orig_global
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_query_all(self):
        for tool in ["memory_search", "memory_save", "memory_search"]:
            audit.enqueue_audit(
                db_path=str(self.db_path),
                tool=tool,
                args="{}",
                latency_ms=0.1,
            )
        audit.flush_audit(timeout=2.0)
        rj = json.loads(self.memory_mcp.memory_audit_query(limit=10))
        self.assertEqual(rj["summary"]["total"], 3)
        self.assertEqual(len(rj["rows"]), 3)

    def test_query_filter_by_tool(self):
        for tool in ["memory_search", "memory_save", "memory_search"]:
            audit.enqueue_audit(
                db_path=str(self.db_path),
                tool=tool,
                args="{}",
                latency_ms=0.1,
            )
        audit.flush_audit(timeout=2.0)
        rj = json.loads(self.memory_mcp.memory_audit_query(tool_name="memory_search"))
        self.assertEqual(rj["summary"]["total"], 2)
        for row in rj["rows"]:
            self.assertEqual(row["tool"], "memory_search")

    def test_query_only_errors(self):
        audit.enqueue_audit(
            db_path=str(self.db_path),
            tool="t1",
            args="{}",
            latency_ms=0.1,
            error="boom",
        )
        audit.enqueue_audit(
            db_path=str(self.db_path),
            tool="t2",
            args="{}",
            latency_ms=0.1,
            error=None,
        )
        audit.flush_audit(timeout=2.0)
        rj = json.loads(self.memory_mcp.memory_audit_query(only_errors=True))
        self.assertEqual(rj["summary"]["total"], 1)
        self.assertEqual(rj["rows"][0]["tool"], "t1")
        self.assertEqual(rj["summary"]["errors"], 1)

    def test_query_invalid_params(self):
        for bad in [
            {"limit": 0},
            {"limit": 600},
            {"offset": -1},
            {"since_ts": 5.0, "until_ts": 1.0},
        ]:
            r = self.memory_mcp.memory_audit_query(**bad)
            self.assertIn("INVALID_PARAMS", r, f"should reject {bad}")

    def test_query_pagination(self):
        for i in range(5):
            audit.enqueue_audit(
                db_path=str(self.db_path),
                tool="t",
                args=f'{{"i":{i}}}',
                latency_ms=0.1,
            )
        audit.flush_audit(timeout=2.0)
        page1 = json.loads(self.memory_mcp.memory_audit_query(limit=2, offset=0))
        page2 = json.loads(self.memory_mcp.memory_audit_query(limit=2, offset=2))
        self.assertEqual(len(page1["rows"]), 2)
        self.assertEqual(len(page2["rows"]), 2)
        self.assertNotEqual(page1["rows"][0]["id"], page2["rows"][0]["id"])


if __name__ == "__main__":
    unittest.main()
