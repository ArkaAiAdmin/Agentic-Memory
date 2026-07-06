#!/usr/bin/env python3
"""Skill decay cron job — decay hit_count for skills not used in 30+ days.

Reads all skills from memory_skills, applies decay to those whose
last_used_at is older than 30 days (or updated_at if last_used_at is NULL).
Decay: hit_count is halved (floor). Skills whose hit_count falls below
the decay threshold (default 0.5, i.e. effectively 0) are deleted.

Run weekly (or less frequently):
    venv/bin/python cron/cron_skill_decay.py [--db PATH] [--max-age-days 30] [--decay-factor 0.5] [--delete-threshold 0.5] [--dry-run]

Or via enqueue_task:
    venv/bin/python cron/enqueue_task.py --task-type cron_skill_decay
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _flock import acquire_lock_or_exit

from infra.infrastructure import resolve_active_memory_dir
from infra.log import setup_logging

logger = setup_logging("cron_skill_decay")

_DEFAULT_MAX_AGE_DAYS = 30
_DEFAULT_DECAY_FACTOR = 0.5
_DEFAULT_DELETE_THRESHOLD = 0.5


def _get_db_path(cli_override: str | None) -> Path:
    if cli_override:
        return Path(cli_override)
    env = os.environ.get("MEMORY_DB_PATH")
    if env:
        return Path(env)
    return resolve_active_memory_dir() / "memory.db"


def _decayed_skills(
    conn, max_age_days: float, decay_factor: float, delete_threshold: float
) -> tuple[list[tuple[int, str, int]], list[str]]:
    """Return (decayed_list, deleted_list).

    decayed_list: [(id, name, new_hit_count)] for skills whose hit_count
        was decayed but still above the delete threshold.
    deleted_list: [name] for skills whose hit_count fell to 0 after decay.
    """
    cutoff = time.time() - max_age_days * 86400
    rows = conn.execute(
        "SELECT id, name, hit_count, last_used_at, updated_at FROM memory_skills"
    ).fetchall()

    decayed: list[tuple[int, str, int]] = []
    deleted: list[str] = []

    for sid, name, hit_count, last_used_at, updated_at in rows:
        ref_ts = last_used_at if last_used_at else updated_at
        if ref_ts is None or ref_ts > cutoff:
            continue
        new_hit = int(hit_count * decay_factor) if hit_count else 0
        if new_hit < delete_threshold:
            deleted.append(name)
        else:
            decayed.append((sid, name, new_hit))
    return decayed, deleted


def _apply_decay(conn, decayed: list[tuple[int, str, int]]) -> int:
    """Write decayed hit_count. Returns count updated."""
    now_ts = time.time()
    for sid, _name, new_hit in decayed:
        conn.execute(
            "UPDATE memory_skills SET hit_count = ?, updated_at = ? WHERE id = ?",
            (new_hit, now_ts, sid),
        )
    conn.commit()
    return len(decayed)


def _delete_skills(conn, names: list[str]) -> int:
    """Delete skills by name. Returns count deleted."""
    if not names:
        return 0
    placeholders = ",".join("?" * len(names))
    conn.execute(
        f"DELETE FROM memory_skills WHERE name IN ({placeholders})", names
    )
    conn.commit()
    return len(names)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Decay stale skill hit_count.")
    parser.add_argument("--db", default=None, help="Path to memory.db")
    parser.add_argument("--max-age-days", type=float, default=_DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--decay-factor", type=float, default=_DEFAULT_DECAY_FACTOR)
    parser.add_argument("--delete-threshold", type=float, default=_DEFAULT_DELETE_THRESHOLD)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    db_path = _get_db_path(args.db)
    if not db_path.exists():
        print(f"ERROR: no memory.db at {db_path}")
        return 1

    acquire_lock_or_exit("cron_skill_decay")

    t0 = time.time()
    try:
        import sqlite3

        conn = sqlite3.connect(str(db_path), timeout=10)
        try:
            decayed, deleted = _decayed_skills(
                conn, args.max_age_days, args.decay_factor, args.delete_threshold
            )
            prefix = "[DRY RUN] " if args.dry_run else ""
            if args.dry_run:
                print(
                    f"{prefix}skill_decay: {len(decayed)} would decay, "
                    f"{len(deleted)} would delete"
                )
                if decayed:
                    for _sid, name, new_hit in decayed[:10]:
                        print(f"  decay: {name} -> hit_count={new_hit}")
                    if len(decayed) > 10:
                        print(f"  ... and {len(decayed) - 10} more")
                if deleted:
                    for name in deleted:
                        print(f"  delete: {name}")
            else:
                n_decayed = _apply_decay(conn, decayed) if decayed else 0
                n_deleted = _delete_skills(conn, deleted) if deleted else 0
                elapsed = time.time() - t0
                print(
                    f"skill_decay: {n_decayed} decayed, {n_deleted} deleted "
                    f"in {elapsed:.2f}s"
                )
        finally:
            conn.close()
    except Exception:
        import traceback

        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
