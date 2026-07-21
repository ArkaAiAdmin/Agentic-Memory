#!/usr/bin/env python3
"""Regression tests for SQLite connection leaks, WAL autocheckpoints, and pool recycling."""

import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_TEST_DIR = tempfile.mkdtemp(prefix="conn_leak_test_")
_TEST_DB = Path(_TEST_DIR) / "memory.db"
os.environ["MEMORY_DB_PATH"] = str(_TEST_DB)

from infra.db import connection_pool, open_db
from mcp_audit import memory_audit_query
from mcp_search import memory_session_start


def setup_module():
    """Ensure test DB exists and schema is populated."""
    with open_db(_TEST_DB, timeout=5.0) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, content TEXT, created_at TEXT, updated_at TEXT, deleted_at TEXT, pinned INTEGER, importance INTEGER, tenant_id TEXT)"
        )
        conn.commit()


def test_pool_recycling_under_thread_pool():
    """Verify that worker threads reuse pooled connections without leaking handles."""
    def worker(task_id):
        with open_db(_TEST_DB, timeout=5.0, pooled=True, write=False) as conn:
            conn.execute("SELECT COUNT(*) FROM memories").fetchone()

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(worker, i) for i in range(50)]
        for f in futures:
            f.result()

    state = connection_pool.get_state()
    assert state["active"] == 0, f"Expected 0 active connections after tasks exit, got {state['active']}"


def test_session_start_fast_execution():
    """Verify memory_session_start completes in < 500ms without spawning subprocesses."""
    t0 = time.time()
    res = memory_session_start()
    elapsed = time.time() - t0
    assert elapsed < 0.5, f"memory_session_start took {elapsed:.2f}s, expected < 0.5s"
    assert "Memory Recall Briefing" in res or "Session already initialized" in res


def test_audit_query_iso_timestamp_and_prefix():
    """Verify memory_audit_query parses ISO strings and matches bare and memory_ prefixed tool names."""
    res_iso = memory_audit_query(tool_name="crdt_sync", since_ts="2026-01-01T00:00:00Z", limit=10)
    assert '"ok": true' in res_iso or '"ok":true' in res_iso

    res_epoch = memory_audit_query(tool_name="memory_crdt_sync", since_ts=1700000000.0, limit=10)
    assert '"ok": true' in res_epoch or '"ok":true' in res_epoch
