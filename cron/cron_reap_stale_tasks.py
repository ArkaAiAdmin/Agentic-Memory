#!/usr/bin/env python3
"""Cron job: reap stale coordination tasks.

Finds tasks that have been 'active' for more than TASK_ABANDON_TIMEOUT
(10 min) and reassigns them to pending (unassigned) so they can be
claimed by a live agent on next session start.

Also cleans up:
- Pending tasks older than 7 days with no activity (abandoned)
- Messages that were never delivered (dead-letter)

Usage:
    python cron_reap_stale_tasks.py [--db <path>] [--dry-run]
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

logger = logging.getLogger(__name__)

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_repo_root)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _flock import acquire_lock_or_exit


def main() -> int:
    acquire_lock_or_exit("cron_reap_stale_tasks")

    from infra.infrastructure import resolve_active_memory_dir
    db_path = str(resolve_active_memory_dir() / "memory.db")
    if not os.path.exists(db_path):
        print("DB not found, skipping")
        return 0

    import sqlite3
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    now = time.time()
    TASK_ABANDON_TIMEOUT = 600  # 10 minutes
    STALE_PENDING_DAYS = 7

    # 1. Reclaim active tasks that are stale (no update in 10 min)
    stale_cutoff = now - TASK_ABANDON_TIMEOUT
    stale_tasks = conn.execute(
        "SELECT id, task_type, assigned_to, description, created_at FROM shared_tasks "
        "WHERE status='active' AND updated_at < ?",
        (stale_cutoff,),
    ).fetchall()

    reclaimed = 0
    for task in stale_tasks:
        conn.execute(
            "UPDATE shared_tasks SET status='pending', assigned_to=NULL, updated_at=? WHERE id=?",
            (now, task["id"]),
        )
        reclaimed += 1
        # Audit the transition
        try:
            conn.execute(
                "INSERT INTO coordination_audit (action, agent_id, target, detail, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                ("task_reaped", "cron_reaper", str(task["id"]),
                 json.dumps({"from_agent": task["assigned_to"], "reason": "stale_active"}), now),
            )
        except Exception:
            pass

    # 2. Abandon pending tasks older than 7 days (no one picked them up)
    stale_pending_cutoff = now - (STALE_PENDING_DAYS * 86400)
    stale_pending = conn.execute(
        "UPDATE shared_tasks SET status='abandoned', updated_at=? "
        "WHERE status='pending' AND created_at < ?",
        (now, stale_pending_cutoff),
    )

    # 3. Dead-letter messages older than 7 days
    from coordination.messaging import process_dead_letters
    dead_lettered = process_dead_letters(conn, max_age_days=7)

    # 4. Release expired file locks
    from coordination.durability import release_stale_locks, cleanup_stale_agents
    locks_released = release_stale_locks(conn)
    agents_cleaned = cleanup_stale_agents(conn)

    conn.commit()
    conn.close()

    result = {
        "reclaimed_active": reclaimed,
        "abandoned_pending": stale_pending.rowcount if stale_pending else 0,
        "dead_lettered": dead_lettered,
        "locks_released": locks_released,
        "agents_cleaned": agents_cleaned,
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
