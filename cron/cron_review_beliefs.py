#!/usr/bin/env python3
"""Cron job: queue stale/low-confidence beliefs for review.

Scans active beliefs with low confidence or that haven't been reviewed
recently, and inserts them into belief_review_queue for agent triage.

Usage:
    python cron_review_beliefs.py [--db <path>] [--dry-run] [--limit N]
"""

from __future__ import annotations

import logging
logger = logging.getLogger(__name__)

from _flock import acquire_lock_or_exit
import argparse
import os
import sys
import time
from infra.memory_config import get_global_memory_dir


_DEFAULT_DB = os.environ.get("MEMORY_DB_PATH") or str(get_global_memory_dir() / "memory.db")
_DEFAULT_LIMIT = 50


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Queue beliefs due for review")
    parser.add_argument("--db", default=_DEFAULT_DB, help="Path to memory.db")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writes")
    parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT, help="Max beliefs to queue")
    return parser.parse_args()


def main() -> int:
    acquire_lock_or_exit("cron_review_beliefs")
    args = _parse_args()
    db = args.db
    if not os.path.exists(db):
        print(f"review_beliefs: DB not found at {db}", file=sys.stderr)
        return 1

    try:
        import sqlite3
        conn = sqlite3.connect(db)
    except Exception as e:
        print(f"review_beliefs: failed to open DB: {e}", file=sys.stderr)
        return 1

    try:
        from belief.belief_lifecycle import get_beliefs_due_for_review

        # PRAGMA check on belief_assertions before tenant enumeration
        ba_cols = [r[1] for r in conn.execute("PRAGMA table_info(belief_assertions)").fetchall()]
        has_ba_tenant = "tenant_id" in ba_cols

        if has_ba_tenant:
            tenant_rows = conn.execute(
                "SELECT DISTINCT tenant_id FROM belief_assertions WHERE tenant_id IS NOT NULL"
            ).fetchall()
            tenants = [r[0] for r in tenant_rows] if tenant_rows else ["default"]
        else:
            tenants = ["default"]

        # PRAGMA check on belief_review_queue
        queue_cols = [r[1] for r in conn.execute("PRAGMA table_info(belief_review_queue)").fetchall()]
        has_queue_tenant = "tenant_id" in queue_cols

        # Global --limit with per-tenant round-robin
        per_tenant_due: dict[str, list[dict]] = {}
        for t in tenants:
            per_tenant_due[t] = get_beliefs_due_for_review(
                conn, staleness_days=30.0, limit=args.limit, tenant_id=t
            )

        due_items: list[tuple[dict, str]] = []
        ptrs = {t: 0 for t in tenants}
        while len(due_items) < args.limit:
            advanced = False
            for t in tenants:
                if ptrs[t] < len(per_tenant_due[t]):
                    due_items.append((per_tenant_due[t][ptrs[t]], t))
                    ptrs[t] += 1
                    advanced = True
                    if len(due_items) >= args.limit:
                        break
            if not advanced:
                break

        now = time.time()
        queued = 0
        for b, t in due_items:
            if not args.dry_run:
                if has_queue_tenant:
                    conn.execute(
                        "INSERT OR IGNORE INTO belief_review_queue "
                        "(belief_id, fact_id, reason, status, created_at, tenant_id) "
                        "VALUES (?, ?, ?, 'pending', ?, ?)",
                        (b["id"], b["fact_id"], "stale_or_low_confidence", now, t),
                    )
                else:
                    conn.execute(
                        "INSERT OR IGNORE INTO belief_review_queue "
                        "(belief_id, fact_id, reason, status, created_at) "
                        "VALUES (?, ?, ?, 'pending', ?)",
                        (b["id"], b["fact_id"], "stale_or_low_confidence", now),
                    )
            queued += 1

        if not args.dry_run:
            conn.commit()

        output = {
            "queued": queued,
            "dry_run": args.dry_run,
        }
        import json
        print(json.dumps(output))
        return 0
    except Exception as e:
        logger.warning("review_beliefs failed: %s", e)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
