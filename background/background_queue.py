"""SQLite-backed background task queue for agentic-memory.

Provides a lightweight, persistent task queue for offloading expensive
operations (entity resolution, fact consolidation, contradiction checks)
to a background worker. All operations are idempotent and crash-safe.

Schema:
    task_queue: pending/processing/completed/failed tasks with payloads

Usage:
    from background_queue import init_task_queue, enqueue_task
    init_task_queue(conn)
    enqueue_task(conn, 'entity_resolution', {'memory_id': 'lessons/foo'})
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "init_task_queue",
    "enqueue_task",
    "dequeue_task",
    "complete_task",
    "fail_task",
    "pending_count",
    "worker_status",
    "cleanup_old_tasks",
]

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_TASK_QUEUE_DDL = """
CREATE TABLE IF NOT EXISTS task_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type   TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',
    priority    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    started_at  TEXT,
    completed_at TEXT,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3
);

CREATE INDEX IF NOT EXISTS idx_task_queue_status ON task_queue(status);
CREATE INDEX IF NOT EXISTS idx_task_queue_type ON task_queue(task_type);
CREATE INDEX IF NOT EXISTS idx_task_queue_priority ON task_queue(priority DESC, created_at ASC);
"""


def init_task_queue(conn: sqlite3.Connection) -> None:
    """Create the task_queue table if it doesn't exist. Idempotent."""
    conn.executescript(_TASK_QUEUE_DDL)


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


def enqueue_task(
    conn: sqlite3.Connection,
    task_type: str,
    payload: dict[str, Any] | None = None,
    priority: int = 0,
) -> int:
    """Insert a pending task into the queue. Returns the task id.

    Duplicate task_type + payload combinations are deduplicated:
    if an identical pending task already exists, its id is returned
    without inserting a new row.
    """
    payload_json = json.dumps(payload or {}, sort_keys=True, default=str)

    in_outer = bool(getattr(conn, "in_transaction", False))
    if in_outer:
        conn.execute("SAVEPOINT enqueue_sp")
        sp_open = True
    else:
        conn.execute("BEGIN IMMEDIATE")
        sp_open = False

    try:
        existing = conn.execute(
            "SELECT id FROM task_queue "
            "WHERE task_type = ? AND payload = ? AND status = 'pending' "
            "LIMIT 1",
            (task_type, payload_json),
        ).fetchone()
        if existing:
            if sp_open:
                conn.execute("RELEASE SAVEPOINT enqueue_sp")
            else:
                conn.commit()
            return int(existing[0])
        cur = conn.execute(
            "INSERT INTO task_queue (task_type, payload, priority) VALUES (?, ?, ?)",
            (task_type, payload_json, priority),
        )
        if sp_open:
            conn.execute("RELEASE SAVEPOINT enqueue_sp")
        else:
            conn.commit()
    except Exception:
        if sp_open:
            conn.execute("ROLLBACK TO SAVEPOINT enqueue_sp")
        else:
            conn.rollback()
        raise
    task_id = cur.lastrowid or 0
    logger.debug("enqueued task %d: %s", task_id, task_type)
    return task_id


# ---------------------------------------------------------------------------
# Dequeue
# ---------------------------------------------------------------------------


def dequeue_task(
    conn: sqlite3.Connection,
    task_type: str | None = None,
) -> dict | None:
    """Atomically fetch and lock the next pending task.

    Uses BEGIN IMMEDIATE to acquire a write lock before SELECT + UPDATE,
    preventing double-processing in multi-worker setups. Returns None if
    no pending tasks exist.

    If task_type is specified, only tasks of that type are returned.

    Nested-transaction safety (2026-06-19): when the caller already has
    a transaction open (e.g. inside a multi-step process_one_task that
    started its own BEGIN), calling BEGIN IMMEDIATE raises
    "cannot start a transaction within a transaction". We detect this
    via ``conn.in_transaction`` and fall back to a SAVEPOINT so the
    dequeue can still run atomically without corrupting the outer
    transaction. The savepoint is released on success and rolled back
    on failure (the outer transaction is left untouched).
    """
    where = "WHERE status = 'pending'"
    params: list[Any] = []
    if task_type:
        where += " AND task_type = ?"
        params.append(task_type)
    # Choose transaction primitive based on outer state.
    #   - No outer txn: BEGIN IMMEDIATE (full reserved lock, serializes workers)
    #   - Outer txn: SAVEPOINT (nested, no lock upgrade)
    in_outer = bool(getattr(conn, "in_transaction", False))
    if in_outer:
        conn.execute("SAVEPOINT dequeue_sp")
        sp_open = True
    else:
        conn.execute("BEGIN IMMEDIATE")
        sp_open = False
    try:
        row = conn.execute(
            f"SELECT id, task_type, payload, priority, attempts, max_attempts "
            f"FROM task_queue {where} "
            f"ORDER BY priority DESC, created_at ASC LIMIT 1",
            params,
        ).fetchone()
        if not row:
            if sp_open:
                conn.execute("RELEASE SAVEPOINT dequeue_sp")
            else:
                conn.commit()
            return None
        task_id, ttype, payload_json, priority, attempts, max_attempts = row
        conn.execute(
            "UPDATE task_queue SET status = 'processing', started_at = datetime('now'), "
            "attempts = attempts + 1 WHERE id = ?",
            (task_id,),
        )
        if sp_open:
            conn.execute("RELEASE SAVEPOINT dequeue_sp")
        else:
            conn.commit()
    except Exception:
        if sp_open:
            try:
                conn.execute("ROLLBACK TO SAVEPOINT dequeue_sp")
                conn.execute("RELEASE SAVEPOINT dequeue_sp")
            except Exception:
                pass
        else:
            conn.rollback()
        raise
    return {
        "id": task_id,
        "task_type": ttype,
        "payload": json.loads(payload_json),
        "priority": priority,
        "attempts": attempts + 1,
        "max_attempts": max_attempts,
    }


# ---------------------------------------------------------------------------
# Complete / Fail
# ---------------------------------------------------------------------------


def complete_task(conn: sqlite3.Connection, task_id: int) -> None:
    """Mark a task as successfully completed."""
    conn.execute(
        "UPDATE task_queue SET status = 'completed', completed_at = datetime('now') "
        "WHERE id = ?",
        (task_id,),
    )
    conn.commit()
    logger.debug("completed task %d", task_id)


def fail_task(conn: sqlite3.Connection, task_id: int, error: str) -> None:
    """Mark a task as failed. Re-enables it if attempts < max_attempts."""
    row = conn.execute(
        "SELECT attempts, max_attempts FROM task_queue WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row and row[0] < row[1]:
        # Retryable: set back to pending
        conn.execute(
            "UPDATE task_queue SET status = 'pending', error = ? WHERE id = ?",
            (error, task_id),
        )
        logger.warning(
            "task %d failed (retryable, attempt %d/%d): %s",
            task_id,
            row[0],
            row[1],
            error,
        )
    else:
        # Exhausted: mark as permanently failed
        conn.execute(
            "UPDATE task_queue SET status = 'failed', error = ? WHERE id = ?",
            (error, task_id),
        )
        logger.error("task %d permanently failed: %s", task_id, error)
    conn.commit()


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def pending_count(conn: sqlite3.Connection, task_type: str | None = None) -> int:
    """Return the number of pending tasks, optionally filtered by type."""
    if task_type:
        row = conn.execute(
            "SELECT COUNT(*) FROM task_queue WHERE status = 'pending' AND task_type = ?",
            (task_type,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM task_queue WHERE status = 'pending'",
        ).fetchone()
    return row[0] if row else 0


def worker_status(conn: sqlite3.Connection) -> dict:
    """Return aggregate task counts by status and type."""
    rows = conn.execute(
        "SELECT status, task_type, COUNT(*) FROM task_queue GROUP BY status, task_type"
    ).fetchall()
    result: dict[str, Any] = {
        "total": 0,
        "by_status": {},
        "by_type": {},
    }
    for status, ttype, cnt in rows:
        result["total"] += cnt
        result["by_status"].setdefault(status, 0)
        result["by_status"][status] += cnt
        result["by_type"].setdefault(ttype, {})
        result["by_type"][ttype][status] = cnt
    return result


def cleanup_old_tasks(conn: sqlite3.Connection, max_age_days: int = 7) -> int:
    """Delete completed/failed tasks older than max_age_days. Returns count deleted."""
    cur = conn.execute(
        "DELETE FROM task_queue "
        "WHERE status IN ('completed', 'failed') "
        "AND completed_at < datetime('now', ?)",
        (f"-{max_age_days} days",),
    )
    deleted = cur.rowcount
    if deleted:
        conn.commit()
        logger.debug("cleaned up %d old tasks", deleted)
    return deleted
