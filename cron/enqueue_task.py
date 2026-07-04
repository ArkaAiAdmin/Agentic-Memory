#!/usr/bin/env python3
"""Cron enqueue wrapper: enqueue a worker task instead of running a cron
script directly.

Usage (from cron):
    python cron/enqueue_task.py --task-type embedding_recompute --payload '{"once": true}'

This is the Phase B consolidation primitive.  Instead of each cron script
running its logic inline (with its own flock, DB connection, etc.), the
cron entry becomes a thin enqueue call and the background worker picks up
the actual work with its existing locking and timeout guarantees.
"""

import argparse
import json
import os
import sys
import time
import datetime as _dt
from pathlib import Path
os.chdir(os.path.dirname(os.path.abspath(__file__)))
_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)

from infra.infrastructure import resolve_active_memory_dir
from infra.db_write_queue import sqlite_write_queue
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection



def _check_debounce(
    conn: AnyConnection, task_type: str, debounce_seconds: int
) -> tuple:
    """Return (is_debounced, reason_str).

    Uses UTC-aware parsing because SQLite ``datetime('now')`` stores UTC.
    """
    if debounce_seconds <= 0:
        return False, ""
    row = conn.execute(
        "SELECT completed_at FROM task_queue "
        "WHERE task_type = ? AND status = 'completed' "
        "ORDER BY completed_at DESC LIMIT 1",
        (task_type,),
    ).fetchone()
    if not row or not row[0]:
        return False, ""
    try:
        completed_dt = _dt.datetime.strptime(
            row[0], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=_dt.timezone.utc)
        completed_ts = completed_dt.timestamp()
    except (ValueError, TypeError):
        return False, ""
    elapsed = time.time() - completed_ts
    if elapsed < debounce_seconds:
        return True, f"last_completed={elapsed:.1f}s ago, debounce={debounce_seconds}s"
    return False, ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Enqueue a background worker task")
    parser.add_argument(
        "--task-type",
        required=True,
        help="Task type string (must match a HANDLERS entry in background_worker.py)",
    )
    parser.add_argument(
        "--payload",
        default="{}",
        help="JSON payload for the task (string or @filepath to load from file)",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Path to memory.db (defaults to MEMORY_DB_PATH or active memory dir)",
    )
    parser.add_argument(
        "--priority",
        type=int,
        default=0,
        help="Task priority (higher runs first; default 0)",
    )
    parser.add_argument(
        "--max-queue-size",
        type=int,
        default=500,
        help="Max pending tasks before rejecting new ones (0=disable; default 500)",
    )
    parser.add_argument(
        "--debounce-seconds",
        type=int,
        default=0,
        help="Skip enqueue if a task of this type completed within this many seconds",
    )
    args = parser.parse_args()

    payload_str = args.payload
    if payload_str.startswith("@"):
        payload_path = Path(payload_str[1:])
        if not payload_path.exists():
            print(f"Error: payload file not found: {payload_path}", file=sys.stderr)
            return 1
        payload_str = payload_path.read_text()

    try:
        payload = json.loads(payload_str) if payload_str.strip() else {}
    except json.JSONDecodeError as exc:
        print(f"Error: invalid JSON payload: {exc}", file=sys.stderr)
        return 1

    db_path = (
        Path(args.db_path)
        if args.db_path
        else (
            Path(os.environ["MEMORY_DB_PATH"])
            if os.environ.get("MEMORY_DB_PATH")
            else resolve_active_memory_dir() / "memory.db"
        )
    )

    if not db_path.exists():
        print(f"Error: database not found at {db_path}", file=sys.stderr)
        return 1

    max_qs = args.max_queue_size

    conn = sqlite_write_queue.start_session(db_path)
    try:
        debounced, debounce_reason = _check_debounce(
            conn, args.task_type, args.debounce_seconds
        )
        if debounced:
            print(f"skipped: debounce ({debounce_reason})")
            return 0

        from background.background_queue import init_task_queue, enqueue_task

        init_task_queue(conn)
        task_id = enqueue_task(
            conn,
            args.task_type,
            payload=payload,
            priority=args.priority,
            max_queue_size=max_qs,
        )
        if isinstance(task_id, dict):
            print(
                f"queued=False reason={task_id.get('reason','?')} "
                f"pending={task_id.get('pending','?')}"
            )
            return 0
        print(f"enqueued task_id={task_id} type={args.task_type}")
        return 0
    except Exception as exc:
        print(f"Error enqueuing task: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
