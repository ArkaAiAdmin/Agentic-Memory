#!/usr/bin/env python3
"""Task queue health monitor — lightweight watchdog for Phase B cron consolidation.

Checks two signals:
  1. Queue depth: warns if total pending tasks exceed a threshold.
  2. Task staleness: warns if a task_type has not completed within its
     expected window (default 24h).

Designed to be called every 15–30 min from cron (runs in <1s, no flock
contention with the worker itself). Exit code is always 0 so cron never
fails the host's mail queue; warnings go to stderr for the cron log.
"""

from __future__ import annotations

import datetime as _dt
import os
import sqlite3
import sys
import time
from pathlib import Path

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_repo_root)
_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)

from infra.infrastructure import resolve_active_memory_dir

WARN_PENDING_THRESHOLD = int(os.environ.get("MEMORY_TASK_QUEUE_WARN_THRESHOLD", "50"))
WARN_STALE_SECONDS = int(os.environ.get("MEMORY_TASK_STALE_THRESHOLD_S", "86400"))


def check(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    warnings: list[str] = []
    try:
        from background.background_queue import init_task_queue, pending_count

        init_task_queue(conn)

        total_pending = pending_count(conn)
        if total_pending >= WARN_PENDING_THRESHOLD:
            warnings.append(
                f"TASK_QUEUE_DEPTH: {total_pending} pending "
                f"(threshold={WARN_PENDING_THRESHOLD})"
            )

        # 2. Stale task types (no completion in >24h)
        rows = conn.execute(
            "SELECT task_type, MAX(completed_at) as last_completed "
            "FROM task_queue WHERE status = 'completed' "
            "GROUP BY task_type"
        ).fetchall()
        now = time.time()
        for task_type, last_completed in rows:
            if not last_completed:
                continue
            try:
                completed_dt = _dt.datetime.strptime(
                    last_completed, "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=_dt.timezone.utc)
                completed_ts = completed_dt.timestamp()
                age = now - completed_ts
                if age > WARN_STALE_SECONDS:
                    hours = age / 3600
                    warnings.append(
                        f"TASK_STALE: {task_type} last completed "
                        f"{hours:.1f}h ago (threshold={WARN_STALE_SECONDS / 3600:.0f}h)"
                    )
            except (ValueError, TypeError):
                pass
    finally:
        conn.close()
    return warnings


def main() -> int:
    db_path = (
        Path(os.environ["MEMORY_DB_PATH"])
        if os.environ.get("MEMORY_DB_PATH")
        else resolve_active_memory_dir() / "memory.db"
    )
    if not db_path.exists():
        print(f"No memory.db at {db_path}", file=sys.stderr)
        return 0

    warnings = check(db_path)
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    if not warnings:
        print("task_queue: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
