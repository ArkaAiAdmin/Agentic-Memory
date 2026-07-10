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


_DEFAULT_DB = "memory/memory.db"
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
        due = get_beliefs_due_for_review(conn, staleness_days=30.0, limit=args.limit)
        now = time.time()
        queued = 0
        for b in due:
            if not args.dry_run:
                conn.execute(
                    "INSERT OR IGNORE INTO belief_review_queue "
                    "(belief_id, fact_id, reason, status, created_at) VALUES (?,?,?, 'pending', ?)",
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
