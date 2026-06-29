"""Tests for background_queue.py — SQLite-backed task queue.

Covers: init, enqueue, dequeue, complete, fail, dedup, worker_status,
cleanup, retry logic.
"""

import os
import sys
import sqlite3
import tempfile
import json

sys.path.insert(0, os.path.expanduser("~/.config/agentic-memory"))

from background_queue import (
    init_task_queue,
    enqueue_task,
    dequeue_task,
    complete_task,
    fail_task,
    pending_count,
    worker_status,
    cleanup_old_tasks,
)


def _make_db():
    """Create a temp SQLite DB with task_queue table and return (conn, path)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    init_task_queue(conn)
    return conn, path


class TestInitTaskQueue:
    """Schema creation."""

    def test_creates_task_queue_table(self):
        conn, path = _make_db()
        try:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='task_queue'"
                ).fetchall()
            ]
            assert "task_queue" in tables
        finally:
            conn.close()
            os.unlink(path)

    def test_idempotent(self):
        conn, path = _make_db()
        try:
            # Call init again — should not fail
            init_task_queue(conn)
            init_task_queue(conn)
        finally:
            conn.close()
            os.unlink(path)


class TestEnqueueTask:
    """Task enqueueing."""

    def test_enqueue_returns_id(self):
        conn, _ = _make_db()
        try:
            task_id = enqueue_task(
                conn, "entity_resolution", {"memory_id": "lessons/foo"}
            )
            assert isinstance(task_id, int)
            assert task_id > 0
        finally:
            conn.close()

    def test_enqueue_stores_payload(self):
        conn, _ = _make_db()
        try:
            payload = {"memory_id": "lessons/foo", "key": "value"}
            task_id = enqueue_task(conn, "entity_resolution", payload)
            row = conn.execute(
                "SELECT task_type, payload, status FROM task_queue WHERE id = ?",
                (task_id,),
            ).fetchone()
            assert row[0] == "entity_resolution"
            assert json.loads(row[1]) == payload
            assert row[2] == "pending"
        finally:
            conn.close()

    def test_enqueue_deduplicates(self):
        conn, _ = _make_db()
        try:
            id1 = enqueue_task(conn, "entity_resolution", {"memory_id": "x"})
            id2 = enqueue_task(conn, "entity_resolution", {"memory_id": "x"})
            assert id1 == id2  # Same id returned
            count = conn.execute("SELECT COUNT(*) FROM task_queue").fetchone()[0]
            assert count == 1  # Only one row
        finally:
            conn.close()

    def test_enqueue_different_payloads_not_deduped(self):
        conn, _ = _make_db()
        try:
            id1 = enqueue_task(conn, "entity_resolution", {"memory_id": "x"})
            id2 = enqueue_task(conn, "entity_resolution", {"memory_id": "y"})
            assert id1 != id2
            count = conn.execute("SELECT COUNT(*) FROM task_queue").fetchone()[0]
            assert count == 2
        finally:
            conn.close()

    def test_enqueue_default_priority(self):
        conn, _ = _make_db()
        try:
            task_id = enqueue_task(conn, "test", {})
            row = conn.execute(
                "SELECT priority FROM task_queue WHERE id = ?", (task_id,)
            ).fetchone()
            assert row[0] == 0
        finally:
            conn.close()

    def test_enqueue_custom_priority(self):
        conn, _ = _make_db()
        try:
            task_id = enqueue_task(conn, "test", {}, priority=5)
            row = conn.execute(
                "SELECT priority FROM task_queue WHERE id = ?", (task_id,)
            ).fetchone()
            assert row[0] == 5
        finally:
            conn.close()


class TestDequeueTask:
    """Task dequeueing."""

    def test_dequeue_returns_task(self):
        conn, _ = _make_db()
        try:
            enqueue_task(conn, "entity_resolution", {"memory_id": "x"})
            task = dequeue_task(conn)
            assert task is not None
            assert task["task_type"] == "entity_resolution"
            assert task["payload"] == {"memory_id": "x"}
            assert task["attempts"] == 1
        finally:
            conn.close()

    def test_dequeue_marks_as_processing(self):
        conn, _ = _make_db()
        try:
            task_id = enqueue_task(conn, "test", {})
            dequeue_task(conn)
            row = conn.execute(
                "SELECT status, started_at FROM task_queue WHERE id = ?",
                (task_id,),
            ).fetchone()
            assert row[0] == "processing"
            assert row[1] is not None
        finally:
            conn.close()

    def test_dequeue_empty_returns_none(self):
        conn, _ = _make_db()
        try:
            task = dequeue_task(conn)
            assert task is None
        finally:
            conn.close()

    def test_dequeue_fifo_within_priority(self):
        conn, _ = _make_db()
        try:
            enqueue_task(conn, "test", {"n": 1})
            enqueue_task(conn, "test", {"n": 2})
            enqueue_task(conn, "test", {"n": 3})
            t1 = dequeue_task(conn)
            t2 = dequeue_task(conn)
            t3 = dequeue_task(conn)
            assert t1 is not None
            assert t2 is not None
            assert t3 is not None
            assert t1["payload"]["n"] == 1
            assert t2["payload"]["n"] == 2
            assert t3["payload"]["n"] == 3
        finally:
            conn.close()

    def test_dequeue_priority_order(self):
        conn, _ = _make_db()
        try:
            enqueue_task(conn, "test", {"p": "low"}, priority=0)
            enqueue_task(conn, "test", {"p": "high"}, priority=10)
            enqueue_task(conn, "test", {"p": "med"}, priority=5)
            t1 = dequeue_task(conn)
            t2 = dequeue_task(conn)
            t3 = dequeue_task(conn)
            assert t1 is not None
            assert t2 is not None
            assert t3 is not None
            assert t1["payload"]["p"] == "high"
            assert t2["payload"]["p"] == "med"
            assert t3["payload"]["p"] == "low"
        finally:
            conn.close()

    def test_dequeue_filters_by_type(self):
        conn, _ = _make_db()
        try:
            enqueue_task(conn, "type_a", {})
            enqueue_task(conn, "type_b", {})
            task = dequeue_task(conn, task_type="type_b")
            assert task is not None
            assert task["task_type"] == "type_b"
        finally:
            conn.close()

    def test_dequeue_increments_attempts(self):
        conn, _ = _make_db()
        try:
            task_id = enqueue_task(conn, "test", {})
            t1 = dequeue_task(conn)
            assert t1 is not None
            assert t1["attempts"] == 1
            # Fail it (goes back to pending)
            fail_task(conn, task_id, "error")
            t2 = dequeue_task(conn)
            assert t2 is not None
            assert t2["attempts"] == 2
        finally:
            conn.close()


class TestCompleteTask:
    """Task completion."""

    def test_complete_sets_status(self):
        conn, _ = _make_db()
        try:
            task_id = enqueue_task(conn, "test", {})
            dequeue_task(conn)
            complete_task(conn, task_id)
            row = conn.execute(
                "SELECT status, completed_at FROM task_queue WHERE id = ?",
                (task_id,),
            ).fetchone()
            assert row[0] == "completed"
            assert row[1] is not None
        finally:
            conn.close()


class TestFailTask:
    """Task failure and retry logic."""

    def test_fail_retryable_on_first_attempt(self):
        conn, _ = _make_db()
        try:
            task_id = enqueue_task(conn, "test", {})
            dequeue_task(conn)  # attempts=1, max_attempts=3
            fail_task(conn, task_id, "something broke")
            row = conn.execute(
                "SELECT status, error FROM task_queue WHERE id = ?",
                (task_id,),
            ).fetchone()
            # Retryable: re-enabled for retry
            assert row[0] == "pending"
            assert row[1] == "something broke"
        finally:
            conn.close()

    def test_fail_sets_error_on_permanent(self):
        conn, _ = _make_db()
        try:
            task_id = enqueue_task(conn, "test", {})
            # Dequeue + fail 3 times (max_attempts=3)
            for _ in range(3):
                dequeue_task(conn)
                fail_task(conn, task_id, "something broke")
            row = conn.execute(
                "SELECT status, error FROM task_queue WHERE id = ?",
                (task_id,),
            ).fetchone()
            assert row[0] == "failed"
            assert row[1] == "something broke"
        finally:
            conn.close()

    def test_fail_retries_if_attempts_left(self):
        conn, _ = _make_db()
        try:
            task_id = enqueue_task(conn, "test", {})
            dequeue_task(conn)  # attempts=1, max_attempts=3
            fail_task(conn, task_id, "error")
            row = conn.execute(
                "SELECT status FROM task_queue WHERE id = ?", (task_id,)
            ).fetchone()
            assert row[0] == "pending"  # Re-enabled for retry
        finally:
            conn.close()

    def test_fail_permanent_after_max_attempts(self):
        conn, _ = _make_db()
        try:
            task_id = enqueue_task(conn, "test", {})
            # Dequeue + fail 3 times
            for _ in range(3):
                dequeue_task(conn)
                fail_task(conn, task_id, "error")
            row = conn.execute(
                "SELECT status FROM task_queue WHERE id = ?", (task_id,)
            ).fetchone()
            assert row[0] == "failed"  # Permanent failure
        finally:
            conn.close()


class TestPendingCount:
    """Pending task counting."""

    def test_pending_count(self):
        conn, _ = _make_db()
        try:
            assert pending_count(conn) == 0
            enqueue_task(conn, "a", {"n": 1})
            enqueue_task(conn, "b", {"n": 2})
            enqueue_task(conn, "a", {"n": 3})
            assert pending_count(conn) == 3
            assert pending_count(conn, task_type="a") == 2
            assert pending_count(conn, task_type="b") == 1
        finally:
            conn.close()


class TestWorkerStatus:
    """Worker status reporting."""

    def test_worker_status_empty(self):
        conn, _ = _make_db()
        try:
            status = worker_status(conn)
            assert status["total"] == 0
            assert status["by_status"] == {}
        finally:
            conn.close()

    def test_worker_status_counts(self):
        conn, _ = _make_db()
        try:
            id1 = enqueue_task(conn, "entity_resolution", {})
            enqueue_task(conn, "fact_consolidation", {})
            dequeue_task(conn, "entity_resolution")
            complete_task(conn, id1)
            status = worker_status(conn)
            assert status["total"] == 2
            assert status["by_status"]["completed"] == 1
            assert status["by_status"]["pending"] == 1
            assert "entity_resolution" in status["by_type"]
        finally:
            conn.close()


class TestCleanupOldTasks:
    """Old task cleanup."""

    def test_cleanup_removes_old_completed(self):
        conn, _ = _make_db()
        try:
            task_id = enqueue_task(conn, "test", {})
            dequeue_task(conn)
            complete_task(conn, task_id)
            # Backdate completed_at to 10 days ago
            conn.execute(
                "UPDATE task_queue SET completed_at = datetime('now', '-10 days') WHERE id = ?",
                (task_id,),
            )
            conn.commit()
            deleted = cleanup_old_tasks(conn, max_age_days=7)
            assert deleted == 1
            count = conn.execute("SELECT COUNT(*) FROM task_queue").fetchone()[0]
            assert count == 0
        finally:
            conn.close()

    def test_cleanup_keeps_recent(self):
        conn, _ = _make_db()
        try:
            task_id = enqueue_task(conn, "test", {})
            dequeue_task(conn)
            complete_task(conn, task_id)
            deleted = cleanup_old_tasks(conn, max_age_days=7)
            assert deleted == 0
            count = conn.execute("SELECT COUNT(*) FROM task_queue").fetchone()[0]
            assert count == 1
        finally:
            conn.close()


class TestDequeueInTransaction:
    """2026-06-19: dequeue_task must work when called from inside an
    outer transaction. The pre-fix version called ``BEGIN IMMEDIATE``
    unconditionally, which raised OperationalError if a transaction was
    already open. The fix uses SAVEPOINT when ``conn.in_transaction``
    is True.

    Covers:
      - Dequeue from inside an outer txn returns the task
      - Outer txn can still commit normally after dequeue
      - Dequeue failure (e.g. malformed payload) rolls back the
        savepoint but leaves the outer txn intact
      - Empty dequeue from inside outer txn commits the savepoint and
        returns None
    """

    def test_dequeue_from_inside_outer_txn_succeeds(self):
        conn, _ = _make_db()
        try:
            enqueue_task(conn, "t1", {"k": "v"})
            conn.commit()
            conn.execute("BEGIN")
            assert conn.in_transaction
            t = dequeue_task(conn, task_type="t1")
            assert t is not None
            assert t["task_type"] == "t1"
            assert t["payload"] == {"k": "v"}
            complete_task(conn, t["id"])
            conn.commit()
        finally:
            conn.close()

    def test_outer_txn_rolls_back_cleanly_after_dequeue(self):
        conn, _ = _make_db()
        try:
            enqueue_task(conn, "t1", {})
            conn.commit()
            conn.execute("BEGIN")
            t = dequeue_task(conn, task_type="t1")
            assert t is not None
            conn.rollback()  # outer txn rolls back; dequeue should be undone
            # Task should be back to pending (dequeue's UPDATE was rolled back)
            count = pending_count(conn, task_type="t1")
            assert count == 1
        finally:
            conn.close()

    def test_empty_dequeue_from_inside_outer_txn(self):
        conn, _ = _make_db()
        try:
            conn.execute("BEGIN")
            t = dequeue_task(conn, task_type="nonexistent")
            assert t is None
            # Outer txn should still be open and commitable.
            assert conn.in_transaction
            conn.commit()
        finally:
            conn.close()

    def test_dequeue_failure_rolls_back_savepoint_only(self):
        """If the dequeue SELECT/UPDATE raises, the savepoint is rolled
        back but the outer transaction is preserved. We trigger a real
        failure by dropping the task_queue table mid-call (can't mock
        sqlite3.Connection.execute because it's a read-only C attribute).
        """
        conn, _ = _make_db()
        try:
            enqueue_task(conn, "t1", {})
            conn.commit()
            conn.execute("BEGIN")
            assert conn.in_transaction

            # Force a real failure inside the savepoint: drop the
            # task_queue table after SAVEPOINT is opened.
            #
            # The cleanest way to do this is to patch at a higher level
            # — wrap the connection in a proxy whose execute() raises
            # on the UPDATE call but not on the SAVEPOINT/RELEASE ones.
            class _BoomProxy:
                """Wraps a sqlite3.Connection and raises on the second
                execute() call inside a dequeue (i.e. the UPDATE)."""

                def __init__(self, inner, raise_on_call_n):
                    self._inner = inner
                    self._n = 0
                    self._raise_on = raise_on_call_n

                @property
                def in_transaction(self):  # type: ignore[override]
                    return self._inner.in_transaction

                def execute(self, sql, params=()):
                    self._n += 1
                    if self._n == self._raise_on:
                        raise RuntimeError("simulated UPDATE failure")
                    return self._inner.execute(sql, params)

                def commit(self):
                    return self._inner.commit()

                def rollback(self):
                    return self._inner.rollback()

            proxy = _BoomProxy(conn, raise_on_call_n=3)  # SAVEPOINT, SELECT, UPDATE
            try:
                dequeue_task(proxy, task_type="t1")  # type: ignore[arg-type]
            except RuntimeError:
                pass
            # Outer txn should still be open (savepoint rolled back, not
            # the whole txn).
            assert conn.in_transaction
            conn.commit()
        finally:
            conn.close()
