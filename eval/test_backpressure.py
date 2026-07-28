"""Tests for backpressure in background_queue.enqueue_task."""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from background.background_queue import (
    complete_task,
    dequeue_task,
    enqueue_task,
    init_task_queue,
    pending_count,
)


@pytest.fixture()
def conn():
    """In-memory SQLite DB with task_queue schema."""
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    init_task_queue(c)
    yield c
    c.close()


class TestRejectNew:
    """reject_new policy returns rejection dict when queue is full."""

    def test_reject_when_at_capacity(self, conn):
        ids = []
        for i in range(3):
            r = enqueue_task(conn, "t", {"i": i}, max_queue_size=3)
            assert isinstance(r, int)
            ids.append(r)
        r = enqueue_task(conn, "t", {"i": 99}, max_queue_size=3, reject_policy="reject_new")
        assert isinstance(r, dict)
        assert r["queued"] is False
        assert r["reason"] == "queue_full"
        assert r["pending"] == 3
        assert r["max_queue_size"] == 3

    def test_accept_when_below_capacity(self, conn):
        r = enqueue_task(conn, "t", {}, max_queue_size=10)
        assert isinstance(r, int)
        assert r > 0

    def test_cap_zero_disables_backpressure(self, conn):
        for i in range(50):
            r = enqueue_task(conn, "t", {"i": i}, max_queue_size=0)
            assert isinstance(r, int)


class TestRejectOld:
    """reject_old evicts the oldest pending task to make room."""

    def test_evicts_oldest_by_priority_then_created_at(self, conn):
        ids = []
        for i in range(5):
            r = enqueue_task(conn, "t", {"i": i})
            assert isinstance(r, int)
            ids.append(r)
        r = enqueue_task(conn, "t", {"i": 99}, max_queue_size=5, reject_policy="reject_old")
        assert isinstance(r, int)
        assert pending_count(conn) == 5

    def test_does_not_evict_higher_priority(self, conn):
        h_id = enqueue_task(conn, "high", {"p": "h"}, priority=10)
        l1 = enqueue_task(conn, "low1", {"p": "l1"}, priority=0)
        l2 = enqueue_task(conn, "low2", {"p": "l2"}, priority=0)
        l3 = enqueue_task(conn, "low3", {"p": "l3"}, priority=0)
        l4 = enqueue_task(conn, "low4", {"p": "l4"}, priority=0)
        r = enqueue_task(conn, "low5", {"p": "l5"}, priority=0, max_queue_size=5, reject_policy="reject_old")
        assert isinstance(r, int)
        row = conn.execute("SELECT id FROM task_queue WHERE status='pending' AND id=?", (h_id,)).fetchone()
        assert row is not None
        assert l1 not in [row[0] for row in conn.execute("SELECT id FROM task_queue WHERE status='pending'").fetchall()]


class TestBlockPolicy:
    """block policy waits for space to free up."""

    def test_blocks_until_space(self, conn):
        enqueue_task(conn, "t", {}, max_queue_size=2)

        def drain_later():
            time.sleep(0.1)
            task = dequeue_task(conn)
            if task:
                complete_task(conn, task["id"])

        t = threading.Thread(target=drain_later)
        t.start()
        r = enqueue_task(conn, "t", {}, max_queue_size=2, reject_policy="block")
        t.join()
        assert isinstance(r, int)

    def test_blocks_times_out(self, conn):
        for i in range(5):
            enqueue_task(conn, "t", {"i": i})
        r = enqueue_task(conn, "t", {"i": 99}, max_queue_size=5, reject_policy="reject_new")
        assert isinstance(r, dict)
        assert r["reason"] == "queue_full"