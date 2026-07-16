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

from infra.memory_config import GLOBAL_MEM_DIR
from infra.db_write_queue import sqlite_write_queue


def _resolve_db() -> Path:
    env_path = os.environ.get("MEMORY_DB_PATH")
    if env_path:
        return Path(env_path)
    # Use GLOBAL_MEM_DIR directly (CWD-independent). The default
    # resolve_active_memory_dir() is CWD-relative and resolves to the
    # wrong dir when this script chdir's into cron/ (see top of file),
    # silently pointing the health check at an empty local DB.
    return GLOBAL_MEM_DIR / "memory.db"


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


# CQRS write-journal backlog guard (Step 8 follow-up). When
# MEMORY_WRITE_JOURNAL_ENABLED is ON, agents enqueue writes to
# journal.db and the background_worker daemon drains them. If the
# daemon is not live (e.g. cron not installed, or it crashed and
# the watchdog hasn't restarted it yet), pending rows accumulate and
# writes are silently deferred. Surface this through the same health
# channel so pipeline-coverage can alert + the daemon can be restarted.
JOURNAL_PENDING_WARN = 50


def _journal_pending_depth(db_path: Path) -> int | None:
    """Count pending rows in journal.db, or None if the journal is off/absent.

    Opens journal.db directly (read-only SELECT) rather than via
    ``sqlite_write_queue.start_session``, because the journal DB carries
    only the ``write_journal`` table and is not managed by the numbered
    migration runner that ``start_session`` would apply.
    """
    import sqlite3

    journal_path = db_path.parent / "journal.db"
    if not journal_path.exists():
        return None
    try:
        jconn = sqlite3.connect(str(journal_path))
        try:
            row = jconn.execute(
                "SELECT COUNT(*) FROM write_journal WHERE status = 'pending'"
            ).fetchone()
            return row[0] if row else 0
        finally:
            jconn.close()
    except Exception:
        # Journal unreachable (locked, corrupt, no table) — treat as unknown.
        return None


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

        failures = _count_failures(conn, hours=24)
        pending = _pending_depth(conn)
        journal_pending = _journal_pending_depth(db_path)

        # Report depth probes BEFORE the sentinel poll so they are always
        # emitted even when the worker is down (poll would raise/timeout).
        # The journal_pending line is the CQRS-backlog signal: a growing
        # count means the background_worker daemon isn't draining journal.db.
        print(f"failed_last_24h: {failures}")
        print(f"pending_queue_depth: {pending}")
        if journal_pending is not None:
            print(f"journal_pending: {journal_pending}")
        else:
            print("journal_pending: n/a (journal off or absent)")

        if journal_pending is not None and journal_pending > JOURNAL_PENDING_WARN:
            print(
                f"WARNING: journal.db has {journal_pending} pending writes "
                f"(> {JOURNAL_PENDING_WARN}); background_worker daemon may not "
                f"be draining the CQRS journal",
                file=sys.stderr,
            )

        _poll_sentinel(conn, task_id, timeout_s=30.0)
        elapsed = time.time() - t0

        print(f"sentinel_completed: elapsed={elapsed:.2f}s")

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
