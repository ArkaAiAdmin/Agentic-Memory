#!/usr/bin/env python3
"""CLI tool to inspect and update per-task-type timeout configuration.

Reads/writes the cron_task_timeouts table (migration 063).

Usage:
    python cron/manage_task_timeouts.py --list
    python cron/manage_task_timeouts.py --get cron_integrity_check
    python cron/manage_task_timeouts.py --set cron_integrity_check --timeout 600 --max-attempts 5
    python cron/manage_task_timeouts.py --reset cron_integrity_check
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from infra.infrastructure import resolve_active_memory_dir


def _get_db_path() -> Path:
    env = os.environ.get("MEMORY_DB_PATH")
    if env:
        return Path(env)
    return resolve_active_memory_dir() / "memory.db"


def _get_conn() -> sqlite3.Connection:
    db_path = _get_db_path()
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def list_timeouts() -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT task_type, timeout_s, max_attempts, auto_retry_after_s, "
            "auto_retry_max_extra FROM cron_task_timeouts ORDER BY task_type"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_timeout(task_type: str) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT task_type, timeout_s, max_attempts, auto_retry_after_s, "
            "auto_retry_max_extra FROM cron_task_timeouts WHERE task_type = ?",
            (task_type,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_timeout(
    task_type: str,
    timeout_s: int | None = None,
    max_attempts: int | None = None,
    auto_retry_after_s: int | None = None,
    auto_retry_max_extra: int | None = None,
) -> dict:
    conn = _get_conn()
    try:
        existing = conn.execute(
            "SELECT * FROM cron_task_timeouts WHERE task_type = ?", (task_type,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO cron_task_timeouts (task_type) VALUES (?)",
                (task_type,),
            )
            existing = conn.execute(
                "SELECT * FROM cron_task_timeouts WHERE task_type = ?",
                (task_type,),
            ).fetchone()
        updates = {}
        if timeout_s is not None:
            updates["timeout_s"] = timeout_s
        if max_attempts is not None:
            updates["max_attempts"] = max_attempts
        if auto_retry_after_s is not None:
            updates["auto_retry_after_s"] = auto_retry_after_s
        if auto_retry_max_extra is not None:
            updates["auto_retry_max_extra"] = auto_retry_max_extra
        if updates:
            clauses = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [task_type]
            conn.execute(
                f"UPDATE cron_task_timeouts SET {clauses}, updated_at = datetime('now') "
                f"WHERE task_type = ?",
                values,
            )
            conn.commit()
        row = conn.execute(
            "SELECT task_type, timeout_s, max_attempts, auto_retry_after_s, "
            "auto_retry_max_extra FROM cron_task_timeouts WHERE task_type = ?",
            (task_type,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def reset_timeout(task_type: str) -> dict:
    conn = _get_conn()
    try:
        conn.execute(
            "DELETE FROM cron_task_timeouts WHERE task_type = ?",
            (task_type,),
        )
        conn.commit()
        return {"task_type": task_type, "status": "reset (row deleted, will fall back to defaults)"}
    finally:
        conn.close()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Manage per-task-type timeouts")
    parser.add_argument("--list", action="store_true", help="List all timeouts")
    parser.add_argument("--get", type=str, default="", help="Get timeout for a task type")
    parser.add_argument("--set", type=str, default="", help="Set timeout for a task type")
    parser.add_argument("--timeout", type=int, default=None, help="Timeout in seconds")
    parser.add_argument("--max-attempts", type=int, default=None, help="Max retry attempts")
    parser.add_argument("--auto-retry-after", type=int, default=None, help="Auto-retry interval seconds")
    parser.add_argument("--auto-retry-max-extra", type=int, default=None, help="Max extra auto-retry rounds")
    parser.add_argument("--reset", type=str, default="", help="Delete timeout entry for a task type")
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    output: dict | list[dict] | None = None

    if args.list:
        output = list_timeouts()
    elif args.get:
        output = get_timeout(args.get)
        if output is None:
            print(f"Task type '{args.get}' not found in cron_task_timeouts")
            return 1
    elif args.set:
        output = set_timeout(
            args.set,
            timeout_s=args.timeout,
            max_attempts=args.max_attempts,
            auto_retry_after_s=args.auto_retry_after,
            auto_retry_max_extra=args.auto_retry_max_extra,
        )
    elif args.reset:
        output = reset_timeout(args.reset)
    else:
        parser.print_help()
        return 0

    if args.json:
        print(json.dumps(output, indent=2, default=str))
    elif isinstance(output, list):
        if not output:
            print("No timeout configurations found.")
            return 0
        print(f"{'Task Type':<35} {'Timeout(s)':<12} {'Max Att':<8} {'Retry(s)':<10} {'Extra Att':<10}")
        print("-" * 75)
        for row in output:
            print(
                f"{row['task_type']:<35} {row['timeout_s']:<12} "
                f"{row['max_attempts']:<8} {row['auto_retry_after_s']:<10} "
                f"{row['auto_retry_max_extra']:<10}"
            )
    elif isinstance(output, dict):
        print(json.dumps(output, indent=2, default=str))

    return 0


if __name__ == "__main__":
    sys.exit(main())
