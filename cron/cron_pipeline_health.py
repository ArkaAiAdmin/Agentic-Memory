"""Pipeline end-to-end health check.

Enqueues a sentinel task and waits for the background worker to pick
it up and complete it (short poll, ~30s max).  Reports:

  - elapsed (s) for sentinel completion
  - total failed tasks in the last 24h
  - pending queue depth

Returns 0 if healthy, 1 if not.
"""

import os
import sys
import time
from pathlib import Path

SENTINEL_TYPE = "cron_pipeline_sentinel"

os.chdir(os.path.dirname(os.path.abspath(__file__)))
_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)

from infra.infrastructure import resolve_active_memory_dir
from infra.db_write_queue import sqlite_write_queue


def _resolve_db() -> Path:
    env_path = os.environ.get("MEMORY_DB_PATH")
    if env_path:
        return Path(env_path)
    return resolve_active_memory_dir() / "memory.db"


def _enqueue_sentinel(conn) -> int:
    from background.background_queue import init_task_queue, enqueue_task

    init_task_queue(conn)
    task_id = enqueue_task(conn, SENTINEL_TYPE, payload={"_sentinel": True})
    if isinstance(task_id, dict):
        raise RuntimeError(f"sentinel enqueue failed: {task_id.get('reason', '?')}")
    return task_id


def _poll_sentinel(conn, task_id: int, timeout_s: float = 30.0) -> float:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        row = conn.execute(
            "SELECT status, completed_at FROM task_queue WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"sentinel task {task_id} not found")
        status = row[0]
        if status == "completed":
            return time.time()
        if status == "failed":
            raise RuntimeError(f"sentinel task {task_id} failed")
        time.sleep(1)
    raise TimeoutError(f"sentinel task {task_id} not completed within {timeout_s}s")


def _count_failures(conn, hours: int = 24) -> int:
    row = conn.execute(
        """SELECT COUNT(*) FROM task_queue
           WHERE status = 'failed'
           AND (completed_at IS NOT NULL
                OR julianday('now') - julianday(created_at) < ? / 24.0)""",
        (hours,),
    ).fetchone()
    return row[0] if row else 0


def _pending_depth(conn) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM task_queue WHERE status = 'pending'"
    ).fetchone()
    return row[0] if row else 0


def main() -> int:
    db_path = _resolve_db()
    if not db_path.exists():
        print(f"CRITICAL: database not found at {db_path}", file=sys.stderr)
        return 1

    conn = sqlite_write_queue.start_session(db_path)
    try:
        t0 = time.time()

        task_id = _enqueue_sentinel(conn)
        print(f"sentinel enqueued: task_id={task_id}")

        _poll_sentinel(conn, task_id, timeout_s=30.0)
        elapsed = time.time() - t0

        failures = _count_failures(conn, hours=24)
        pending = _pending_depth(conn)

        print(f"sentinel_completed: elapsed={elapsed:.2f}s")
        print(f"failed_last_24h: {failures}")
        print(f"pending_queue_depth: {pending}")

        if elapsed > 20.0:
            print(f"WARNING: sentinel took {elapsed:.1f}s (>20s threshold)", file=sys.stderr)
            return 1

        return 0
    except Exception as exc:
        print(f"CRITICAL: pipeline health check failed: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
