"""Sprint 5: MCP session tool tests.

Tests the 3 CORE tools defined in mcp_session.py:
  - memory_thread_context
  - memory_list_threads
  - memory_resolve_thread

Uses temp DBs with v22 schema applied via _make_db helper.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from infra.migration_runner import run_migrations
from session_manager import SessionManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "test_sm.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    run_migrations(conn)
    conn.close()
    return db


def _enable_session_flag(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMORY_SESSION_MEMORY", "1")


def _reset_config():
    import config as _cfg_mod

    _cfg_mod.reset_config()


def _import_tools():
    import importlib

    return importlib.import_module("mcp_surface.mcp_session")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _session_env(tmp_path, monkeypatch):
    _enable_session_flag(monkeypatch)
    _reset_config()
    db = _make_db(tmp_path)
    monkeypatch.setenv("MEMORY_DB_PATH", str(db))
    mgr = SessionManager(db_path=db)
    ctx = mgr.start_session("/tmp/proj", agent_id="test_agent")
    assert ctx is not None
    return db, mgr, ctx


def _seed_thread(db: Path, session_id: str, title: str, status: str = "open") -> str:
    tid = f"thread_{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO decision_threads (id, session_id, title, status, created_at, version_vector) "
        "VALUES (?, ?, ?, ?, datetime('now'), ?)",
        (tid, session_id, title, status, "{}"),
    )
    conn.commit()
    conn.close()
    return tid


def _seed_event(
    db: Path, session_id: str, thread_id: str, event_type: str, content: str
):
    eid = f"evt_{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO thread_events (id, thread_id, session_id, seq, event_type, content, content_summary, created_at, version_vector) "
        "VALUES (?, ?, ?, 1, ?, ?, ?, datetime('now'), ?)",
        (eid, thread_id, session_id, event_type, content, content[:300], "{}"),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# memory_thread_context
# ---------------------------------------------------------------------------


class TestMemoryThreadContext:
    def test_returns_threads_for_session(self, _session_env):
        db, mgr, ctx = _session_env
        _seed_thread(db, ctx.session.id, "DB choice")

        mod = _import_tools()
        raw = mod.memory_thread_context(session_id=ctx.session.id)
        data = json.loads(raw)
        assert data["session_id"] == ctx.session.id
        assert len(data["threads"]) >= 1
        assert data["threads"][0]["title"] == "DB choice"

    def test_filters_by_thread_id(self, _session_env):
        db, mgr, ctx = _session_env
        tid = _seed_thread(db, ctx.session.id, "Target thread")
        _seed_thread(db, ctx.session.id, "Other thread")

        mod = _import_tools()
        raw = mod.memory_thread_context(session_id=ctx.session.id, thread_id=tid)
        data = json.loads(raw)
        assert len(data["threads"]) == 1
        assert data["threads"][0]["id"] == tid

    def test_includes_events(self, _session_env):
        db, mgr, ctx = _session_env
        tid = _seed_thread(db, ctx.session.id, "With events")
        _seed_event(db, ctx.session.id, tid, "decision", "We chose PostgreSQL.")

        mod = _import_tools()
        raw = mod.memory_thread_context(
            session_id=ctx.session.id, include_events=True, event_limit=5
        )
        data = json.loads(raw)
        thread = data["threads"][0]
        assert len(thread["events"]) >= 1
        assert thread["events"][0]["event_type"] == "decision"

    def test_no_session_id_returns_error(self):
        mod = _import_tools()
        raw = mod.memory_thread_context()
        assert raw.startswith("Error") or "error" in raw


# ---------------------------------------------------------------------------
# memory_list_threads
# ---------------------------------------------------------------------------


class TestMemoryListThreads:
    def test_lists_open_threads(self, _session_env):
        db, mgr, ctx = _session_env
        _seed_thread(db, ctx.session.id, "Open thread", "open")
        _seed_thread(db, ctx.session.id, "Resolved thread", "resolved")

        mod = _import_tools()
        raw = mod.memory_list_threads(session_id=ctx.session.id)
        data = json.loads(raw)
        titles = [t["title"] for t in data["threads"]]
        assert "Open thread" in titles
        assert "Resolved thread" not in titles

    def test_respects_limit(self, _session_env):
        db, mgr, ctx = _session_env
        for i in range(5):
            _seed_thread(db, ctx.session.id, f"Thread {i}", "open")

        mod = _import_tools()
        raw = mod.memory_list_threads(session_id=ctx.session.id, limit=2)
        data = json.loads(raw)
        assert len(data["threads"]) == 2

    def test_no_session_id_returns_error(self):
        mod = _import_tools()
        raw = mod.memory_list_threads()
        assert raw.startswith("Error") or "error" in raw


# ---------------------------------------------------------------------------
# memory_resolve_thread
# ---------------------------------------------------------------------------


class TestMemoryResolveThread:
    def test_resolves_thread(self, _session_env):
        db, mgr, ctx = _session_env
        tid = _seed_thread(db, ctx.session.id, "To resolve", "open")

        mod = _import_tools()
        raw = mod.memory_resolve_thread(
            thread_id=tid, resolution="Done", superseded_by=""
        )
        data = json.loads(raw)
        assert data.get("ok") is True

        conn = sqlite3.connect(db)
        conn.execute("PRAGMA foreign_keys=ON")
        row = conn.execute(
            "SELECT status, resolved_at FROM decision_threads WHERE id=?", (tid,)
        ).fetchone()
        conn.close()
        assert row[0] == "resolved"
        assert row[1] is not None

    def test_missing_thread_id_returns_error(self):
        mod = _import_tools()
        raw = mod.memory_resolve_thread(thread_id="")
        assert raw.startswith("Error") or "error" in raw
