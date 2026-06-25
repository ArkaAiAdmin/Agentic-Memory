#!/usr/bin/env python3
"""Pinned-note auto-decay check.

Reads the active memory DB, identifies pinned notes that are drifting,
and applies two policies:

  - Auto-unpin (DB-only):  psi > 60 AND days_since_last_access > 180
  - Flag for review:       psi > 30 AND days_since_last_access > 365

Definitions
-----------
psi = days_since_updated / max(1, access_count)
   Higher psi => more drift. A note with many accesses and recent
   updates has low psi; a note with few accesses and stale updates
   has high psi.

days_since_last_access: days since the last read of the note.
   Falls back to `updated_at` if `last_accessed` is NULL
   (e.g. legacy rows before the migration).

We auto-unpin rather than auto-delete so the note remains in the DB
and can be re-pinned if the user wants. The note's `pinned` column
goes 1 -> 0; nothing else changes.

Why no frontmatter rewrite
--------------------------
Setting `pinned: false` in the frontmatter is left for the next
rebuild cycle; touching files mid-session is a recipe for races.

CLI:
    python pinned_decay.py [--dry-run] [--json] [--auto-apply]

By default, runs in dry-run mode (report only). Pass --auto-apply
to actually unpin. Always pass --json for machine-readable output.
"""

__all__ = ["check", "main"]
import argparse
import datetime
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from memory_config import GLOBAL_MEM_DIR

UNPIN_PSI = 60.0
UNPIN_DAYS = 180
REVIEW_PSI = 30.0
REVIEW_DAYS = 365


def resolve_db():
    """Return path to the active memory DB, respecting overrides."""
    env_path = os.environ.get("MEMORY_DB_PATH")
    if env_path:
        return Path(env_path)
    try:
        from infrastructure import resolve_active_memory_dir

        return resolve_active_memory_dir() / "memory.db"
    except ImportError:
        return GLOBAL_MEM_DIR / "memory.db"


def _parse_dt(s: str):
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        else:
            dt = dt.astimezone(datetime.timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def check(dry_run: bool = True, db_path: Optional[Path] = None) -> dict:
    """Run the decay check; return a report dict.

    If dry_run is False, auto-unpins matches. Always returns the
    candidate list so the user can see what would happen.
    """
    db_path = db_path or resolve_db()
    if not db_path.exists():
        return {"error": f"no DB at {db_path}"}
    from _lazy_imports import connection_pool

    conn = connection_pool.get(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    if "last_accessed" not in cols:
        return {
            "error": "last_accessed column missing — run migrate_last_accessed.py first"
        }

    now = datetime.datetime.now(datetime.timezone.utc)
    rows = conn.execute(
        """SELECT id, pinned, fitness_score, access_count, success_score,
                  created_at, updated_at, observed_at, last_accessed, importance_score
           FROM memories WHERE pinned = 1"""
    ).fetchall()
    col_names = [
        "id",
        "pinned",
        "fitness_score",
        "access_count",
        "success_score",
        "created_at",
        "updated_at",
        "observed_at",
        "last_accessed",
        "importance_score",
    ]
    metrics = [dict(zip(col_names, r)) for r in rows]

    auto_unpin = []
    review = []
    for m in metrics:
        updated = _parse_dt(m["updated_at"])
        last_acc = _parse_dt(m["last_accessed"]) or updated
        if last_acc is None:
            last_acc = _parse_dt(m["created_at"])
        if last_acc is None:
            continue
        days_since_access = (now - last_acc).days
        access_count = m["access_count"] or 0
        psi = days_since_access / max(1, access_count)
        m["psi"] = round(psi, 2)
        m["days_since_access"] = days_since_access
        m["last_accessed"] = last_acc.isoformat(timespec="seconds")
        if psi > UNPIN_PSI and days_since_access > UNPIN_DAYS:
            auto_unpin.append(m)
        elif psi > REVIEW_PSI and days_since_access > REVIEW_DAYS:
            review.append(m)

    unpinned_ids = []
    if auto_unpin and not dry_run:
        for m in auto_unpin:
            conn.execute("UPDATE memories SET pinned = 0 WHERE id = ?", (m["id"],))
            unpinned_ids.append(m["id"])
        conn.commit()

    # Summary stats
    pinned_total = len(metrics)
    health_summary = {
        "pinned_total": pinned_total,
        "auto_unpin_candidates": len(auto_unpin),
        "review_candidates": len(review),
        "unpinned": unpinned_ids if not dry_run else [],
    }

    # Trim fields for output
    def _trim(m):
        return {
            "id": m["id"],
            "psi": m["psi"],
            "days_since_access": m["days_since_access"],
            "access_count": m["access_count"],
            "last_accessed": m["last_accessed"],
            "importance": m["importance_score"],
            "fitness_score": round(m["fitness_score"] or 0.0, 2),
        }

    return {
        "dry_run": dry_run,
        "db": str(db_path),
        "policies": {
            "unpin": f"psi > {UNPIN_PSI} AND days > {UNPIN_DAYS}",
            "review": f"psi > {REVIEW_PSI} AND days > {REVIEW_DAYS}",
        },
        "summary": health_summary,
        "auto_unpin": [_trim(m) for m in sorted(auto_unpin, key=lambda x: -x["psi"])],
        "review": [_trim(m) for m in sorted(review, key=lambda x: -x["psi"])],
    }


def main():
    p = argparse.ArgumentParser(prog="pinned_decay")
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="(default) Report only; do not unpin anything",
    )
    p.add_argument(
        "--auto-apply",
        action="store_true",
        help="Actually unpin matching notes (opposite of --dry-run)",
    )
    p.add_argument("--json", action="store_true", help="Force JSON output")
    args = p.parse_args()
    dry_run = not args.auto_apply
    report = check(dry_run=dry_run)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    # C5 fix: distinguish three states for cron monitoring.
    #   0 = nothing to do (dry run, or no decay candidates)
    #   1 = decay candidates were detected (caller may want to alert)
    #   2 = decay was actually applied (auto_apply path)
    if not dry_run and report.get("summary", {}).get("unpinned"):
        sys.exit(2)  # applied auto-decay
    if report.get("summary", {}).get("unpinned"):
        sys.exit(1)  # candidates exist (dry-run with findings)
    sys.exit(0)


if __name__ == "__main__":
    main()
