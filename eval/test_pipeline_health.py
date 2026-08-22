"""Tests for pipeline health check (Step 6).

Tests:
  - sentinel enqueue + poll
  - sentinel handler registered in HANDLERS
  - failure counting
  - pending depth
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from background.background_queue import init_task_queue, enqueue_task


def _plain_conn(db_path: Path) -> sqlite3.Connection:
    """Open a plain sqlite3 connection suitable for test fixtures.

    Avoids the threaded SQLiteWriteQueue singleton which can timeout
    under pytest's rapid fixture teardown/creation cycles.
    """
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@pytest.fixture
def db_path() -> Path:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    yield path
    if path.exists():
        path.unlink()


@pytest.fixture
def conn(db_path):
    conn = _plain_conn(db_path)
    init_task_queue(conn)
    yield conn
    conn.close()


class TestPipelineHealth:
    def test_sentinel_handler_registered(self):
        from background.background_worker import HANDLERS
        assert "cron_pipeline_sentinel" in HANDLERS

    def test_sentinel_handler_returns_healthy(self):
        from background.background_worker import HANDLERS
        result = HANDLERS["cron_pipeline_sentinel"]({}, None, Path("."))
        assert result == "pipeline_healthy"

    def test_enqueue_and_process_sentinel(self, db_path):
        conn = _plain_conn(db_path)
        init_task_queue(conn)
        task_id = enqueue_task(conn, "cron_pipeline_sentinel", payload={"_sentinel": True})
        assert isinstance(task_id, int)
        conn.close()

        from background.background_worker import process_one_task
        conn2 = _plain_conn(db_path)
        ok = process_one_task(conn2, db_path)
        assert ok is True
        row = conn2.execute(
            "SELECT status FROM task_queue WHERE id = ?", (task_id,)
        ).fetchone()
        assert row[0] == "completed"
        conn2.close()

    def test_pending_depth_zero_when_empty(self, conn):
        from cron.cron_pipeline_health import _pending_depth
        assert _pending_depth(conn) == 0

    def test_pending_depth_counts_pending(self, conn):
        enqueue_task(conn, "cron_pipeline_sentinel", payload={"i": 1})
        enqueue_task(conn, "cron_pipeline_sentinel", payload={"i": 2})
        from cron.cron_pipeline_health import _pending_depth
        assert _pending_depth(conn) == 2

    def test_count_failures_returns_zero(self, conn):
        from cron.cron_pipeline_health import _count_failures
        assert _count_failures(conn, hours=24) == 0

    def test_count_failures_counts_failed(self, conn):
        for i in range(3):
            task_id = enqueue_task(conn, "cron_pipeline_sentinel", payload={"i": i})
            conn.execute(
                "UPDATE task_queue SET status = 'failed', error = 'test' WHERE id = ?",
                (task_id,),
            )
        conn.commit()
        from cron.cron_pipeline_health import _count_failures
        assert _count_failures(conn, hours=24) == 3

    def test_enqueue_sentinel_returns_int(self, conn):
        from cron.cron_pipeline_health import _enqueue_sentinel
        task_id = _enqueue_sentinel(conn)
        assert isinstance(task_id, int)

    def test_previous_sentinel_completed(self, conn):
        from cron.cron_pipeline_health import (
            _previous_sentinel_outcome,
            SENTINEL_TYPE,
        )

        task_id = enqueue_task(conn, SENTINEL_TYPE, payload={})
        conn.execute(
            "UPDATE task_queue SET status = 'completed', "
            "completed_at = datetime('now', '-60 seconds') "
            "WHERE id = ?",
            (task_id,),
        )
        # Backdate creation so latency is well-defined (~60s).
        conn.execute(
            "UPDATE task_queue SET created_at = datetime('now', '-120 seconds') "
            "WHERE id = ?",
            (task_id,),
        )
        conn.commit()
        out = _previous_sentinel_outcome(conn)
        assert out["state"] == "completed"
        assert out["latency_s"] is not None and 30 < out["latency_s"] < 120
        assert out["task_id"] == task_id

    def test_previous_sentinel_none_on_empty_queue(self, conn):
        from cron.cron_pipeline_health import _previous_sentinel_outcome

        out = _previous_sentinel_outcome(conn)
        assert out["state"] == "none"

    def test_previous_sentinel_pending_with_age(self, conn):
        from cron.cron_pipeline_health import _previous_sentinel_outcome

        task_id = enqueue_task(conn, SENTINEL_TYPE if False else "cron_pipeline_sentinel", payload={})
        conn.execute(
            "UPDATE task_queue SET created_at = datetime('now', '-10 minutes') "
            "WHERE id = ?",
            (task_id,),
        )
        conn.commit()
        out = _previous_sentinel_outcome(conn)
        assert out["state"] == "pending"
        assert out["age_s"] is not None and 500 < out["age_s"] < 700

    def test_previous_sentinel_failed(self, conn):
        from cron.cron_pipeline_health import _previous_sentinel_outcome

        task_id = enqueue_task(conn, "cron_pipeline_sentinel", payload={})
        conn.execute("UPDATE task_queue SET status='failed' WHERE id = ?", (task_id,))
        conn.commit()
        out = _previous_sentinel_outcome(conn)
        assert out["state"] == "failed"

    def test_resolve_db_path_from_env(self, monkeypatch, tmp_path):
        d = tmp_path / "test.db"
        d.write_text("")
        monkeypatch.setenv("MEMORY_DB_PATH", str(d))
        from cron.cron_pipeline_health import _resolve_db
        assert _resolve_db() == d


class TestJournalBacklogProbe:
    """Step 8 follow-up: surface a stalled CQRS write-journal.

    When MEMORY_WRITE_JOURNAL_ENABLED is ON, agents enqueue writes to
    journal.db and the background_worker daemon drains them. If the daemon
    is not live, pending rows accumulate. The probe must count them so
    pipeline-coverage can alert, and return None when the journal is
    absent (flag off) so it never false-positives.
    """

    def test_absent_journal_returns_none(self, tmp_path):
        from cron.cron_pipeline_health import _journal_pending_depth

        db = tmp_path / "memory.db"
        db.write_text("")
        assert _journal_pending_depth(db) is None

    def test_counts_pending_rows(self, tmp_path):
        from cron.cron_pipeline_health import _journal_pending_depth
        from infra.write_journal import init_journal_db

        db = tmp_path / "memory.db"
        db.write_text("")
        journal = tmp_path / "journal.db"
        init_journal_db(journal)

        jconn = _plain_conn(journal)
        try:
            for _ in range(3):
                jconn.execute(
                    "INSERT INTO write_journal "
                    "(note_id, agent_id, category, title_slug, content, status) "
                    "VALUES ('n', 'a', 'lessons', 's', 'c', 'pending')"
                )
            jconn.execute(
                "INSERT INTO write_journal "
                "(note_id, agent_id, category, title_slug, content, status) "
                "VALUES ('n', 'a', 'lessons', 's', 'c', 'applied')"
            )
            jconn.commit()
        finally:
            jconn.close()

        assert _journal_pending_depth(db) == 3

    def test_main_reports_journal_pending(self, monkeypatch, tmp_path, capsys):
        from cron.cron_pipeline_health import main
        from infra.write_journal import init_journal_db

        db = tmp_path / "memory.db"
        _c = _plain_conn(db)
        init_task_queue(_c)
        _c.close()
        journal = tmp_path / "journal.db"
        init_journal_db(journal)
        jconn = _plain_conn(journal)
        try:
            for _ in range(2):
                jconn.execute(
                    "INSERT INTO write_journal "
                    "(note_id, agent_id, category, title_slug, content, status) "
                    "VALUES ('n', 'a', 'lessons', 's', 'c', 'pending')"
                )
            jconn.commit()
        finally:
            jconn.close()

        monkeypatch.setenv("MEMORY_DB_PATH", str(db))
        monkeypatch.setenv("MEMORY_DB_FLOCK", "0")
        # Seed a FAILED previous sentinel: main() must still print the
        # journal_pending depth line before reporting the failure.
        _t = enqueue_task(_c2 := _plain_conn(db), "cron_pipeline_sentinel", payload={})
        _c2.execute("UPDATE task_queue SET status='failed' WHERE id = ?", (_t,))
        _c2.commit()
        _c2.close()

        import cron.cron_pipeline_health as _cph

        monkeypatch.setattr(_cph, "acquire_lock_or_exit", lambda *_a, **_kw: None)

        rc = main()
        assert rc != 0  # previous sentinel failed → pipeline unhealthy
        captured = capsys.readouterr()
        assert "journal_pending: 2" in captured.out


class TestWorkerRecovery:
    """Self-healing: when the worker is down, the health check tries to
    (re)start it via launchd (preferred) or a direct spawn (fallback).
    Recovery is best-effort and must NOT mask a real failure.
    """

    def test_recover_spawns_once_drain(self, monkeypatch, tmp_path):
        import cron.cron_pipeline_health as cph

        spawned = {}

        class _FakePopen:
            def __init__(self, args, **kw):
                spawned["args"] = list(args)

        monkeypatch.setattr("subprocess.Popen", _FakePopen)

        ok = cph._try_start_worker()
        assert ok is True
        assert spawned["args"][-2:] == ["--once", "--max-tasks=50"]

    def test_recover_returns_false_when_all_fail(self, monkeypatch, tmp_path):
        import cron.cron_pipeline_health as cph

        def _fake_Popen(*a, **k):
            raise OSError("boom")

        monkeypatch.setattr("subprocess.Popen", _fake_Popen)

        assert cph._try_start_worker() is False

    def test_main_attempts_recovery_on_stale_sentinel(self, monkeypatch, tmp_path, capsys):
        import cron.cron_pipeline_health as cph

        db = tmp_path / "memory.db"
        _c = _plain_conn(db)
        init_task_queue(_c)
        # Seed a STALE pending sentinel (older than STALE_PENDING_S).
        t = enqueue_task(_c, "cron_pipeline_sentinel", payload={})
        _c.execute(
            "UPDATE task_queue SET created_at = datetime('now', '-1 hour') "
            "WHERE id = ?",
            (t,),
        )
        _c.commit()
        _c.close()
        monkeypatch.setenv("MEMORY_DB_PATH", str(db))
        monkeypatch.setenv("MEMORY_DB_FLOCK", "0")

        recovered = {}

        def _fake_recover():
            recovered["called"] = True
            # Mirror the real _try_start_worker: emit the recover: line.
            import sys as _sys

            print("recover: test-stub recovery invoked", file=_sys.stderr)
            return True

        def _worker_down():
            return False

        monkeypatch.setattr(cph, "_try_start_worker", _fake_recover)
        monkeypatch.setattr(cph, "_worker_alive", _worker_down)
        monkeypatch.setattr(cph, "acquire_lock_or_exit", lambda *_a, **_kw: None)

        rc = cph.main()
        assert rc != 0  # stale sentinel → pipeline unhealthy
        assert recovered.get("called") is True
        captured = capsys.readouterr()
        assert "recover:" in captured.err
