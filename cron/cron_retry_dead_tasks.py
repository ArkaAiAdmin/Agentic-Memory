#!/usr/bin/env python3
"""Auto-retry permanently failed tasks from the task_queue.

Finds tasks that have exhausted their max_attempts (status='failed')
and re-enqueues them if auto-retry is configured for their task_type
in the cron_task_timeouts table (migration 063).

Each task type gets up to auto_retry_max_extra extra retry rounds,
spaced by auto_retry_after_s seconds (jittered, see JITTER_MIN/MAX)
after the previous failure. The jitter spreads retries for a burst of
same-type failures so they don't all fire at once.

Usage:
    python cron/cron_retry_dead_tasks.py          # normal run
    python cron/cron_retry_dead_tasks.py --dry-run  # preview only
    python cron/cron_retry_dead_tasks.py --json      # JSON output
"""

from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cron._flock import acquire_lock_or_exit
from infra.infrastructure import resolve_active_memory_dir

# Jitter factor applied to auto_retry_after_s so that a burst of failures
# for the same task type does not retry in lockstep (thundering herd).
# Effective wait = auto_retry_after_s * uniform(JITTER_MIN, JITTER_MAX).
JITTER_MIN = float(os.environ.get("MEMORY_RETRY_JITTER_MIN", "0.5"))
JITTER_MAX = float(os.environ.get("MEMORY_RETRY_JITTER_MAX", "1.5"))


def _retry_wait_s(base: int) -> float:
    """Effective retry wait (seconds) for a base auto_retry_after_s, with jitter."""
    return base * random.uniform(JITTER_MIN, JITTER_MAX)


def _get_db_path() -> Path:
    env = os.environ.get("MEMORY_DB_PATH")
    if env:
        return Path(env)
    return resolve_active_memory_dir() / "memory.db"


def retry_dead_tasks(
    dry_run: bool = False,
) -> dict:
    """Find permanently failed tasks and re-enqueue them if auto-retry is configured.

    Returns a summary dict with counts of re-enqueued tasks per type.
    """
    import sqlite3

    db_path = _get_db_path()
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row

    summary: dict[str, int] = {}
    errors: list[str] = []

    try:
        # Find permanently failed tasks with their task type and completion time
        failed = conn.execute(
            "SELECT t.id, t.task_type, t.payload, t.priority, t.completed_at, "
            "t.error, t.extra_retry_count, "
            "ct.auto_retry_after_s, ct.auto_retry_max_extra "
            "FROM task_queue t "
            "LEFT JOIN cron_task_timeouts ct ON t.task_type = ct.task_type "
            "WHERE t.status = 'failed'"
        ).fetchall()

        for row in failed:
            task_id = row["id"]
            task_type = row["task_type"]
            extra_retry_count = row["extra_retry_count"] or 0
            auto_retry_after_s = row["auto_retry_after_s"]
            auto_retry_max_extra = row["auto_retry_max_extra"]

            # Skip if no auto-retry config for this type
            if auto_retry_after_s is None or auto_retry_max_extra is None:
                continue

            # Skip if already at max extra retries
            if extra_retry_count >= auto_retry_max_extra:
                continue

            # Check if enough time has passed since the task failed.
            # Use SQLite's datetime comparison to avoid timezone issues.
            # The wait window is jittered (auto_retry_after_s * uniform
            # factor) so a burst of same-type failures retries spread out
            # rather than in lockstep.
            completed_at_str = row["completed_at"]
            if completed_at_str:
                wait_s = _retry_wait_s(auto_retry_after_s)
                row_still_fresh = conn.execute(
                    "SELECT 1 WHERE datetime(?) > datetime('now', ?)",
                    (completed_at_str, f"-{wait_s} seconds"),
                ).fetchone()
                if row_still_fresh:
                    continue

            if dry_run:
                summary[task_type] = summary.get(task_type, 0) + 1
                continue

            # Re-enqueue: create a new task with attempts=0, increment extra_retry_count
            try:
                payload = json.loads(row["payload"]) if row["payload"] else {}
                payload["enqueue_source"] = "cron_retry"
                payload["retried_from_task_id"] = task_id
                payload["previous_error"] = row["error"] or ""

                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO task_queue "
                    "(task_type, payload, status, priority, attempts, extra_retry_count, created_at) "
                    "VALUES (?, ?, 'pending', ?, 0, ?, datetime('now'))",
                    (
                        task_type,
                        json.dumps(payload, default=str),
                        row["priority"],
                        extra_retry_count + 1,
                    ),
                )
                conn.execute(
                    "UPDATE task_queue SET status = 'completed' WHERE id = ? AND status = 'failed'",
                    (task_id,),
                )
                conn.commit()

                summary[task_type] = summary.get(task_type, 0) + 1

            except Exception as e:
                conn.rollback()
                errors.append(str(e))

    finally:
        conn.close()

    return {
        "re_enqueued": summary,
        "total": sum(summary.values()),
        "errors": errors,
    }


def main() -> int:
    acquire_lock_or_exit("cron_retry_dead_tasks")

    args = [a for a in sys.argv[1:] if not a.startswith("--py-")]
    dry_run = "--dry-run" in args
    json_output = "--json" in args

    result = retry_dead_tasks(dry_run=dry_run)

    if json_output:
        print(json.dumps(result, indent=2, default=str))
    else:
        total = result["total"]
        if dry_run:
            print(f"DRY RUN: would re-enqueue {total} task(s): {json.dumps(result['re_enqueued'])}")
        else:
            print(f"Re-enqueued {total} task(s): {json.dumps(result['re_enqueued'])}")

    if result["errors"]:
        print(f"Errors: {len(result['errors'])}", file=sys.stderr)
        for err in result["errors"]:
            print(f"  {err}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
