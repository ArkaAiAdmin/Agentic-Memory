"""Sprint 7: Session/thread admin tool tests.

Tests the new memory_maintenance operations:
  - session_stats
  - thread_stats
  - compaction_stats
  - list_active_threads
  - recover_session
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


def _seed_sessions(db: Path, count: int = 2, status: str = "active") -> list[str]:
    sids = []
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    for i in range(count):
        sid = f"sess_{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO sessions (id, started_at, status, version_vector) VALUES (?, datetime('now'), ?, ?)",
            (sid, status, "{}"),
        )
        sids.append(sid)
    conn.commit()
    conn.close()
    return sids


def _seed_threads(
    db: Path, session_id: str, count: int = 2, status: str = "open"
) -> list[str]:
    tids = []
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    for i in range(count):
        tid = f"thread_{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO decision_threads (id, session_id, title, status, created_at, version_vector) "
            "VALUES (?, ?, ?, ?, datetime('now'), ?)",
            (tid, session_id, f"Thread {i}", status, "{}"),
        )
        tids.append(tid)
    conn.commit()
    conn.close()
    return tids


def _seed_compaction(
    db: Path, session_id: str, tokens_before: int = 5000, tokens_after: int = 2000
):
    cid = f"comp_{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO session_compaction_log (id, session_id, compacted_at, tokens_before, tokens_after, recovered_note_ids, version_vector) "
        "VALUES (?, ?, datetime('now'), ?, ?, ?, ?)",
        (cid, session_id, tokens_before, tokens_after, "[]", "{}"),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _admin_env(tmp_path, monkeypatch):
    _enable_session_flag(monkeypatch)
    _reset_config()
    from infra.memory_common import reset_rate_limiter as _rl_reset
    _rl_reset()
    db = _make_db(tmp_path)
    monkeypatch.setenv("MEMORY_DB_PATH", str(db))
    return db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSessionStats:
    def test_returns_total_and_by_status(self, _admin_env):
        from mcp_maintenance import memory_maintenance

        db = _admin_env
        mgr = SessionManager(db_path=db)
        ctx = mgr.start_session("/tmp/proj", agent_id="a1")
        assert ctx is not None
        raw = memory_maintenance("session_admin_stats", type="session")
        data = json.loads(raw)["sessions"]
        assert "total" in data
        assert "by_status" in data
        assert data["total"] >= 1


class TestThreadStats:
    def test_returns_by_status(self, _admin_env):
        from mcp_maintenance import memory_maintenance

        db = _admin_env
        mgr = SessionManager(db_path=db)
        ctx = mgr.start_session("/tmp/proj", agent_id="a1")
        tid = f"thread_{uuid.uuid4().hex[:8]}"
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO decision_threads (id, session_id, title, status, created_at, version_vector) "
            "VALUES (?, ?, ?, ?, datetime('now'), ?)",
            (tid, ctx.session.id, "T1", "open", "{}"),
        )
        conn.commit()
        conn.close()
        raw = memory_maintenance("session_admin_stats", type="thread")
        data = json.loads(raw)["decision_threads"]
        assert "by_status" in data
        assert "open" in data["by_status"]


class TestCompactionStats:
    def test_returns_counts_and_zombies(self, _admin_env):
        from mcp_maintenance import memory_maintenance

        db = _admin_env
        mgr = SessionManager(db_path=db)
        ctx = mgr.start_session("/tmp/proj", agent_id="a1")
        _seed_compaction(db, ctx.session.id)
        raw = memory_maintenance("session_admin_stats", type="compaction")
        data = json.loads(raw)["compactions"]
        assert "total_compactions" in data
        assert "avg_token_delta" in data
        assert "zombie_sessions" in data


class TestListActiveThreads:
    def test_lists_threads(self, _admin_env):
        from mcp_maintenance import memory_maintenance

        db = _admin_env
        mgr = SessionManager(db_path=db)
        ctx = mgr.start_session("/tmp/proj", agent_id="a1")
        _seed_threads(db, ctx.session.id, count=1, status="open")[0]
        raw = memory_maintenance("list_active_threads")
        data = json.loads(raw)
        assert "threads" in data
        assert len(data["threads"]) >= 1

    def test_filters_by_status(self, _admin_env):
        from mcp_maintenance import memory_maintenance

        db = _admin_env
        mgr = SessionManager(db_path=db)
        ctx = mgr.start_session("/tmp/proj", agent_id="a1")
        _seed_threads(db, ctx.session.id, count=1, status="resolved")
        raw = memory_maintenance("list_active_threads", status="resolved")
        data = json.loads(raw)
        assert all(t["status"] == "resolved" for t in data["threads"])


class TestRecoverSession:
    def test_recovers_chain(self, _admin_env):
        from mcp_maintenance import memory_maintenance

        db = _admin_env
        mgr = SessionManager(db_path=db)
        parent = mgr.start_session("/tmp/proj", agent_id="a1")
        assert parent is not None
        child = mgr.start_session(
            "/tmp/proj",
            agent_id="a1",
            parent_session_id=parent.session.id,
        )
        assert child is not None
        raw = memory_maintenance("recover_session", session_id=child.session.id)
        data = json.loads(raw)
        assert "chain" in data
        assert len(data["chain"]) >= 1
        assert data["chain"][0]["session_id"] == child.session.id
        # parent should be in chain
        ids = [c["session_id"] for c in data["chain"]]
        assert parent.session.id in ids
