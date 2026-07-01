"""Integration tests for the auto-save pipeline (Phases 1–4).

Tests end-to-end through the sync save path from the MCP/CLI entry point to
the DB write, verifying:
  - allowlist/denylist gating
  - circuit-breaker short-circuit
  - save creates a note in the DB
  - sync fallback preserves data
  - _upsert_memory idempotency (same content → single row)
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval._fixtures import bootstrap_temp_db_clean  # noqa: E402


def _make_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def _config_env(memory_db: Path, **overrides) -> dict:
    env = {
        **os.environ,
        "MEMORY_DB_PATH": str(memory_db),
        "MEMORY_ASYNC_AUTOSAVE": "0",
        "AUTO_SAVE_TOOL_ALLOWLIST": "*",
    }
    env.update(overrides)
    return env


def _call_tool_complete(
    tool: str,
    params: str = "{}",
    result_preview: str = "",
    env: dict | None = None,
) -> dict:
    from background.tool_complete import tool_complete

    effective_env = env or os.environ
    # Merge extra env vars into os.environ for the duration of the call
    extras = {k: v for k, v in effective_env.items() if os.environ.get(k) != v}
    old = {}
    for k, v in extras.items():
        old[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        return tool_complete(tool, params, result_preview)
    finally:
        for k in extras:
            if old[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old[k]


class TestAutoSaveSyncSave(TestCase):
    """Tests the synchronous save path."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "memory.db"
        bootstrap_temp_db_clean(self.db_path)
        self.env = _config_env(self.db_path)
        from background.circuit_breaker import _auto_save_reset_state
        _auto_save_reset_state()

    def tearDown(self) -> None:
        from config import _instance

        _instance = None
        from db import connection_pool

        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        connection_pool._migrated.clear()
        from background.circuit_breaker import _auto_save_reset_state
        _auto_save_reset_state()

    def test_sync_save_creates_note_in_db(self) -> None:
        result = _call_tool_complete(
            "memory_save", '{"content":"integration-test-content","category":"lessons"}',
            "preview", self.env,
        )
        assert result.get("saved") or result.get("saved") == "queued"
        conn = sqlite3.connect(str(self.db_path))
        row = conn.execute(
            "SELECT content FROM memories WHERE content LIKE '%integration-test-content%'"
        ).fetchone()
        conn.close()
        assert row is not None, "Expected note not found in DB"

    def test_sync_save_returns_note_id(self) -> None:
        result = _call_tool_complete(
            "memory_save",
            '{"content":"note-id-test","category":"sessions"}',
            "preview",
            self.env,
        )
        assert result.get("saved") or result.get("saved") == "queued"
        assert "note_id" in result
        assert result["note_id"]

    def test_sync_save_allows_allowlisted_tool(self) -> None:
        env = dict(self.env)
        env["AUTO_SAVE_TOOL_ALLOWLIST"] = "memory_save,bash"
        result = _call_tool_complete("memory_save", '{"content":"allowlisted"}', "p", env)
        assert result.get("saved") or result.get("saved") == "queued"

    def test_sync_save_skips_denylisted_tool(self) -> None:
        env = dict(self.env)
        env["AUTO_SAVE_TOOL_ALLOWLIST"] = "memory_save"
        env["AUTO_SAVE_TOOL_DENYLIST"] = "bash,web_search"
        result = _call_tool_complete("bash", '{"command":"ls"}', "p", env)
        assert result.get("skipped") or (not result.get("saved"))

    def test_sync_save_twice_idempotent(self) -> None:
        result1 = _call_tool_complete(
            "memory_save", '{"content":"idempotent-test-content","category":"lessons"}',
            "p", self.env,
        )
        result2 = _call_tool_complete(
            "memory_save", '{"content":"idempotent-test-content","category":"lessons"}',
            "p", self.env,
        )
        assert bool(result1.get("saved")) or result1.get("saved") == "queued"
        assert result2.get("saved") or result2.get("saved") == "queued" or result2.get("skipped")
        conn = sqlite3.connect(str(self.db_path))
        rows = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE content LIKE '%idempotent-test-content%'"
        ).fetchone()[0]
        conn.close()
        assert rows == 1, f"Expected exactly 1 DB row for identical saves, got {rows}"


class TestAutoSaveResilience(TestCase):
    """Circuit-breaker and backpressure behaviour."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "memory.db"
        bootstrap_temp_db_clean(self.db_path)
        from background.circuit_breaker import _auto_save_reset_state
        _auto_save_reset_state()

    def tearDown(self) -> None:
        from config import _instance
        _instance = None
        from background.circuit_breaker import _auto_save_reset_state
        _auto_save_reset_state()
        from db import connection_pool
        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        connection_pool._migrated.clear()

    def test_circuit_trips_after_failures(self) -> None:
        from background.circuit_breaker import (
            _auto_save_record_failure_and_maybe_trip, _auto_save_circuit_open,
        )
        for _ in range(5):
            _auto_save_record_failure_and_maybe_trip()
        assert _auto_save_circuit_open()

    def test_circuit_resets_after_success(self) -> None:
        from background.circuit_breaker import (
            _auto_save_record_success, _auto_save_circuit_open,
            _auto_save_record_failure_and_maybe_trip,
        )
        for _ in range(5):
            _auto_save_record_failure_and_maybe_trip()
        assert _auto_save_circuit_open()
        _auto_save_record_success()
        assert not _auto_save_circuit_open()

    def test_tool_complete_short_circuits_when_circuit_open(self) -> None:
        from background.circuit_breaker import (
            _auto_save_record_failure_and_maybe_trip,
        )
        for _ in range(5):
            _auto_save_record_failure_and_maybe_trip()
        env = _config_env(self.db_path, AUTO_SAVE_TOOL_ALLOWLIST="*")
        result = _call_tool_complete("memory_save", '{"x": 1}', "p", env)
        assert result.get("skipped"), f"Expected skipped, got {result}"
        assert result.get("reason") == "circuit_breaker_open"

    def test_sync_save_persists_across_connections(self) -> None:
        env = _config_env(self.db_path)
        result = _call_tool_complete(
            "memory_save",
            '{"content":"cr-test-content","category":"lessons"}',
            "p", env,
        )
        assert result.get("saved") or result.get("saved") == "queued"
        conn2 = sqlite3.connect(str(self.db_path))
        row = conn2.execute(
            "SELECT content FROM memories WHERE content LIKE '%cr-test-content%'"
        ).fetchone()
        conn2.close()
        assert row is not None

    def test_upsert_does_not_corrupt_existing_note(self) -> None:
        content = "idempotent-note-v1"
        env = _config_env(self.db_path)
        result = _call_tool_complete(
            "memory_save", f'{{"content":"{content}","category":"lessons"}}', "p", env,
        )
        assert result.get("saved") or result.get("saved") == "queued"
        _call_tool_complete(
            "memory_save", f'{{"content":"{content}","category":"lessons"}}', "p", env,
        )
        conn = sqlite3.connect(str(self.db_path))
        all_rows = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE content LIKE ?", (f"%{content}%",)
        ).fetchone()[0]
        conn.close()
        assert all_rows == 1, f"Expected 1 row, found {all_rows}"
