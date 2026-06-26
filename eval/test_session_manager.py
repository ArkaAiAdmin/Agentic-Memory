"""Sprint 2: SessionManager unit tests.

All tests use a temp DB.  Requires the v22 migration to be applied
(handled by temp_db_path fixture via bootstrap_temp_db).
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

import pytest

from migration_runner import run_migrations
from session_manager import SessionManager, _is_enabled, _scrub_metadata


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


def _count_system_rows(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    counts: dict[str, int] = {}
    for table in (
        "sessions",
        "decision_threads",
        "thread_events",
        "session_compaction_log",
    ):
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        counts[table] = row[0] if row else 0
    conn.close()
    return counts


def _enable_session_flag(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMORY_SESSION_MEMORY", "1")


def _disable_session_flag(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMORY_SESSION_MEMORY", "0")


def _wait_for_cleanup():
    from session_manager import _save_system_record

    # give the background save a tick to settle
    time.sleep(0.05)


# ---------------------------------------------------------------------------
# PII scrub
# ---------------------------------------------------------------------------


class TestScrubMetadata:
    def test_strips_password_key(self):
        assert "password" not in _scrub_metadata({"password": "secret123"})

    def test_strips_token_key_case_insensitive(self):
        assert "token" not in _scrub_metadata({"auth_token": "abc"})

    def test_strips_api_key(self):
        assert "api_key" not in _scrub_metadata({"api_key": "key123"})

    def test_strips_secret(self):
        assert "secret" not in _scrub_metadata({"secret": "shhh"})

    def test_preserves_other_keys(self):
        result = _scrub_metadata({"title": "foo", "count": 42, "tags": ["a"]})
        assert result == {"title": "foo", "count": 42, "tags": ["a"]}

    def test_empty_input(self):
        assert _scrub_metadata(None) == {}
        assert _scrub_metadata({}) == {}

    def test_nested_scrub(self):
        d = {"outer": {"password": "x", "safe": "ok"}}
        result = _scrub_metadata(d)
        assert "password" not in result["outer"]
        assert result["outer"]["safe"] == "ok"


# ---------------------------------------------------------------------------
# start_session
# ---------------------------------------------------------------------------


class TestStartSession:
    def test_creates_new_session(self, tmp_path, monkeypatch):
        _enable_session_flag(monkeypatch)
        # reset config singleton so flag change is picked up
        import config as _cfg_mod

        _cfg_mod.reset_config()

        db = _make_db(tmp_path)
        mgr = SessionManager(db_path=db)
        ctx = mgr.start_session("/tmp/project", agent_id="agent-1")

        assert ctx is not None
        assert ctx.session.status == "active"
        assert ctx.session.project_root == "/tmp/project"
        assert ctx.session.agent_id == "agent-1"
        assert ctx.session.id.startswith("sess_")
        assert ctx.active_threads == []
        assert ctx.recent_events == {}

    def test_resumes_active_session(self, tmp_path, monkeypatch):
        _enable_session_flag(monkeypatch)
        import config as _cfg_mod

        _cfg_mod.reset_config()

        db = _make_db(tmp_path)
        mgr = SessionManager(db_path=db)
        ctx1 = mgr.start_session("/tmp/project", agent_id="agent-1")
        assert ctx1 is not None

        ctx2 = mgr.start_session("/tmp/project", agent_id="agent-1")
        assert ctx2 is not None
        assert ctx2.session.id == ctx1.session.id  # same session resumed

    def test_disabled_returns_none(self, tmp_path, monkeypatch):
        _disable_session_flag(monkeypatch)
        import config as _cfg_mod

        _cfg_mod.reset_config()

        db = _make_db(tmp_path)
        mgr = SessionManager(db_path=db)
        ctx = mgr.start_session("/tmp/project", agent_id="agent-1")
        assert ctx is None


# ---------------------------------------------------------------------------
# record_event
# ---------------------------------------------------------------------------


class TestRecordEvent:
    def test_appends_event_with_correct_seq(self, tmp_path, monkeypatch):
        _enable_session_flag(monkeypatch)
        import config as _cfg_mod

        _cfg_mod.reset_config()

        db = _make_db(tmp_path)
        mgr = SessionManager(db_path=db)
        ctx = mgr.start_session("/tmp/proj", agent_id="a1")
        assert ctx is not None

        # Create a thread manually to attach events to
        thread_id = f"thread_{uuid.uuid4().hex[:8]}"
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO decision_threads (id, session_id, title, status, created_at) VALUES (?,?,?,?,?)",
            (thread_id, ctx.session.id, "test thread", "open", mgr._now()),
        )
        conn.commit()
        conn.close()

        e1 = mgr.record_event(ctx.session.id, thread_id, "decision", "chose A")
        e2 = mgr.record_event(ctx.session.id, thread_id, "evidence", "data shows X")

        assert e1 is not None
        assert e2 is not None
        assert e1.seq == 1
        assert e2.seq == 2
        assert e1.event_type == "decision"
        assert e2.content == "data shows X"

    def test_content_summary_truncated(self, tmp_path, monkeypatch):
        _enable_session_flag(monkeypatch)
        import config as _cfg_mod

        _cfg_mod.reset_config()

        db = _make_db(tmp_path)
        mgr = SessionManager(db_path=db)
        ctx = mgr.start_session("/tmp/proj", agent_id="a1")
        assert ctx is not None

        thread_id = f"thread_{uuid.uuid4().hex[:8]}"
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO decision_threads (id, session_id, title, status, created_at) VALUES (?,?,?,?,?)",
            (thread_id, ctx.session.id, "t", "open", mgr._now()),
        )
        conn.commit()
        conn.close()

        long_content = "x" * 500
        e = mgr.record_event(ctx.session.id, thread_id, "claim", long_content)
        assert e is not None
        assert len(e.content_summary) <= 300

    def test_invalid_event_type_rejected(self, tmp_path, monkeypatch):
        _enable_session_flag(monkeypatch)
        import config as _cfg_mod

        _cfg_mod.reset_config()

        db = _make_db(tmp_path)
        mgr = SessionManager(db_path=db)
        ctx = mgr.start_session("/tmp/proj", agent_id="a1")
        assert ctx is not None

        result = mgr.record_event(ctx.session.id, "t1", "bogus_type", "content")
        assert result is None


# ---------------------------------------------------------------------------
# resolve_thread
# ---------------------------------------------------------------------------


class TestResolveThread:
    def test_marks_resolved(self, tmp_path, monkeypatch):
        _enable_session_flag(monkeypatch)
        import config as _cfg_mod

        _cfg_mod.reset_config()

        db = _make_db(tmp_path)
        mgr = SessionManager(db_path=db)
        ctx = mgr.start_session("/tmp/proj", agent_id="a1")
        assert ctx is not None

        thread_id = f"thread_{uuid.uuid4().hex[:8]}"
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO decision_threads (id, session_id, title, status, created_at) VALUES (?,?,?,?,?)",
            (thread_id, ctx.session.id, "t", "open", mgr._now()),
        )
        conn.commit()
        conn.close()

        ok = mgr.resolve_thread(thread_id, resolution="went with option A")
        assert ok is True

        conn = sqlite3.connect(db)
        conn.execute("PRAGMA foreign_keys=ON")
        row = conn.execute(
            "SELECT status, resolved_at FROM decision_threads WHERE id=?", (thread_id,)
        ).fetchone()
        conn.close()
        assert row[0] == "resolved"
        assert row[1] is not None


# ---------------------------------------------------------------------------
# end_session
# ---------------------------------------------------------------------------


class TestEndSession:
    def test_sets_ended_status_and_summary_note(self, tmp_path, monkeypatch):
        _enable_session_flag(monkeypatch)
        import config as _cfg_mod

        _cfg_mod.reset_config()

        db = _make_db(tmp_path)
        mgr = SessionManager(db_path=db)
        ctx = mgr.start_session("/tmp/proj", agent_id="a1")
        assert ctx is not None

        ok = mgr.end_session(ctx.session.id, summary="did stuff, learned things")
        assert ok is True

        conn = sqlite3.connect(db)
        conn.execute("PRAGMA foreign_keys=ON")
        row = conn.execute(
            "SELECT status, ended_at, summary_note_id FROM sessions WHERE id=?",
            (ctx.session.id,),
        ).fetchone()
        conn.close()
        assert row[0] == "ended"
        assert row[1] is not None
        assert row[2] is not None  # summary note written via save_memory

    def test_defers_open_threads(self, tmp_path, monkeypatch):
        _enable_session_flag(monkeypatch)
        import config as _cfg_mod

        _cfg_mod.reset_config()

        db = _make_db(tmp_path)
        mgr = SessionManager(db_path=db)
        ctx = mgr.start_session("/tmp/proj", agent_id="a1")

        tid = f"thread_{uuid.uuid4().hex[:8]}"
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO decision_threads (id, session_id, title, status, created_at) VALUES (?,?,?,?,?)",
            (tid, ctx.session.id, "open question", "open", mgr._now()),
        )
        conn.commit()
        conn.close()

        mgr.end_session(ctx.session.id, summary="wrap up")

        conn = sqlite3.connect(db)
        conn.execute("PRAGMA foreign_keys=ON")
        row = conn.execute(
            "SELECT status FROM decision_threads WHERE id=?", (tid,)
        ).fetchone()
        conn.close()
        assert row[0] == "deferred"


# ---------------------------------------------------------------------------
# compact_session
# ---------------------------------------------------------------------------


class TestCompactSession:
    def test_logs_compaction(self, tmp_path, monkeypatch):
        _enable_session_flag(monkeypatch)
        import config as _cfg_mod

        _cfg_mod.reset_config()

        db = _make_db(tmp_path)
        mgr = SessionManager(db_path=db)
        ctx = mgr.start_session("/tmp/proj", agent_id="a1")

        ok = mgr.compact_session(
            ctx.session.id,
            tokens_before=4000,
            tokens_after=1000,
            summary_note_id=None,
            recovered_note_ids=["n1", "n2"],
        )
        assert ok is True

        conn = sqlite3.connect(db)
        conn.execute("PRAGMA foreign_keys=ON")
        row = conn.execute(
            "SELECT tokens_before, tokens_after, summary_note_id, recovered_note_ids "
            "FROM session_compaction_log WHERE session_id=?",
            (ctx.session.id,),
        ).fetchone()
        conn.close()
        assert row[0] == 4000
        assert row[1] == 1000
        assert row[2] is None
        ids = json.loads(row[3])
        assert ids == ["n1", "n2"]


# ---------------------------------------------------------------------------
# Feature-flag gating
# ---------------------------------------------------------------------------


class TestFeatureFlagGating:
    def test_all_methods_return_none_false_when_disabled(self, tmp_path, monkeypatch):
        _disable_session_flag(monkeypatch)
        import config as _cfg_mod

        _cfg_mod.reset_config()

        db = _make_db(tmp_path)
        mgr = SessionManager(db_path=db)
        ctx = mgr.start_session("/tmp/proj", agent_id="a1")
        assert ctx is None
        assert mgr.record_event("s1", "t1", "decision", "x") is None
        assert mgr.resolve_thread("t1") is False
        assert mgr.end_session("s1") is False
        assert mgr.compact_session("s1") is False
