"""Tests for pipeline health check (Step 6).

Tests:
  - sentinel enqueue + poll
  - sentinel handler registered in HANDLERS
  - failure counting
  - pending depth
"""

import tempfile
from pathlib import Path

import pytest

from background.background_queue import init_task_queue, enqueue_task
from infra.db_write_queue import sqlite_write_queue


@pytest.fixture
def db_path() -> Path:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    yield path
    if path.exists():
        path.unlink()


@pytest.fixture
def conn(db_path):
    conn = sqlite_write_queue.start_session(db_path)
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
        conn = sqlite_write_queue.start_session(db_path)
        init_task_queue(conn)
        task_id = enqueue_task(conn, "cron_pipeline_sentinel", payload={"_sentinel": True})
        assert isinstance(task_id, int)
        conn.close()

        from background.background_worker import process_one_task
        conn2 = sqlite_write_queue.start_session(db_path)
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

    def test_poll_sentinel_completed(self, conn):
        task_id = enqueue_task(conn, "cron_pipeline_sentinel", payload={})
        conn.execute(
            "UPDATE task_queue SET status = 'completed' WHERE id = ?",
            (task_id,),
        )
        conn.commit()
        from cron.cron_pipeline_health import _poll_sentinel
        result = _poll_sentinel(conn, task_id, timeout_s=5)
        assert result > 0

    def test_poll_sentinel_timeout(self, conn):
        task_id = enqueue_task(conn, "cron_pipeline_sentinel", payload={})
        from cron.cron_pipeline_health import _poll_sentinel
        with pytest.raises(TimeoutError):
            _poll_sentinel(conn, task_id, timeout_s=1)

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

        jconn = sqlite_write_queue.start_session(journal)
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
        init_task_queue(sqlite_write_queue.start_session(db))
        journal = tmp_path / "journal.db"
        init_journal_db(journal)
        jconn = sqlite_write_queue.start_session(journal)
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
        # Force the sentinel poll to fail immediately (no worker draining),
        # so main() returns 1 but still prints the journal_pending line.
        def _boom(*_a, **_k):
            raise TimeoutError("no worker")

        import cron.cron_pipeline_health as _cph

        monkeypatch.setattr(_cph, "_poll_sentinel", _boom)

        rc = main()
        assert rc != 0  # sentinel never completed (no worker)
        captured = capsys.readouterr()
        assert "journal_pending: 2" in captured.out
