#!/usr/bin/env python3
import os
import sys
import json
import sqlite3
import datetime
from pathlib import Path
from typing import Any



# Use canonical parse_frontmatter from memory_common (handles CRLF + multi-line values).
from memory_common import parse_frontmatter, find_project_root, safe_close_db  # noqa: E402


class SpacedRepetition:
    def __init__(self, db_path):
        # P1-14 fix: route through the connection pool instead of
        # opening a raw sqlite3.connect(). The previous bare
        # ``sqlite3.connect(str(db_path))`` + bare ``conn.close()`` did
        # not participate in the per-path connection pool, leading to
        # connection leaks under load. ``self.db`` is now a pooled
        # connection; ``close()`` returns it to the pool rather than
        # closing it. Callers that depend on ``self.db`` being a
        # ``sqlite3.Connection`` (it still is) are unaffected.
        from db import connection_pool

        self.db = connection_pool.get(str(db_path), timeout=10.0)
        self.db.execute("PRAGMA busy_timeout = 30000;")
        self._ensure_table()

    def _ensure_table(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS review_schedule (
                memory_id TEXT PRIMARY KEY,
                retrieval_count INTEGER DEFAULT 0,
                interval_days REAL DEFAULT 1.0,
                next_review TEXT NOT NULL,
                last_reviewed TEXT,
                ease_factor REAL DEFAULT 2.5
            )
        """)
        self.db.commit()

    def record_success(self, memory_id):
        """Record successful retrieval. Increase interval exponentially."""
        row = self.db.execute(
            "SELECT retrieval_count, interval_days, ease_factor FROM review_schedule WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()

        if row:
            rc, interval, ef = row
            # SM-2: interval *= ease_factor
            new_interval = min(interval * ef, 180)  # Cap at 180 days
            new_ef = min(ef + 0.1, 3.0)  # Increase ease, cap at 3.0
            new_rc = rc + 1
        else:
            new_interval = 1.0
            new_ef = 2.5
            new_rc = 1

        next_review = (
            datetime.date.today() + datetime.timedelta(days=int(new_interval))
        ).isoformat()
        self.db.execute(
            """
            INSERT OR REPLACE INTO review_schedule
            (memory_id, retrieval_count, interval_days, next_review, last_reviewed, ease_factor)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                memory_id,
                new_rc,
                new_interval,
                next_review,
                datetime.date.today().isoformat(),
                new_ef,
            ),
        )
        self.db.commit()

    def record_failure(self, memory_id):
        """Record failed retrieval. Reset interval, decrease ease factor."""
        # Read current EF to apply SM-2 decrease
        row = self.db.execute(
            "SELECT ease_factor FROM review_schedule WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        current_ef = row[0] if row else 2.5
        # SM-2: decrease EF by 0.2, minimum 1.3
        new_ef = max(current_ef - 0.2, 1.3)
        self.db.execute(
            """
            INSERT OR REPLACE INTO review_schedule
            (memory_id, retrieval_count, interval_days, next_review, last_reviewed, ease_factor)
            VALUES (?, 0, 1.0, ?, ?, ?)
        """,
            (
                memory_id,
                (datetime.date.today() + datetime.timedelta(days=1)).isoformat(),
                datetime.date.today().isoformat(),
                new_ef,
            ),
        )
        self.db.commit()

    def get_due_reviews(self, limit=10):
        """Get memories that are due for review."""
        today = datetime.date.today().isoformat()
        return self.db.execute(
            """
            SELECT memory_id, next_review, interval_days, ease_factor
            FROM review_schedule
            WHERE next_review <= ?
            ORDER BY next_review
            LIMIT ?
        """,
            (today, limit),
        ).fetchall()

    def get_stats(self):
        """Get review statistics."""
        total = self.db.execute("SELECT COUNT(*) FROM review_schedule").fetchone()[0]
        due = self.db.execute(
            "SELECT COUNT(*) FROM review_schedule WHERE next_review <= ?",
            (datetime.date.today().isoformat(),),
        ).fetchone()[0]
        avg_ef = self.db.execute(
            "SELECT AVG(ease_factor) FROM review_schedule"
        ).fetchone()[0]
        return {
            "total_scheduled": total,
            "due_for_review": due,
            "avg_ease_factor": round(avg_ef, 2) if avg_ef else 2.5,
        }

    def close(self):
        safe_close_db(self.db)


if __name__ == "__main__":
    db_env = os.environ.get("MEMORY_DB_PATH")
    if db_env:
        db_path = Path(db_env)
    else:
        from memory_config import get_memory_paths

        _, local_mem, _ = get_memory_paths()
        db_path = local_mem / "memory.db"

    sr = SpacedRepetition(db_path)
    stats = sr.get_stats()
    print(f"Spaced Repetition Stats:")
    print(f"  Total scheduled: {stats['total_scheduled']}")
    print(f"  Due for review: {stats['due_for_review']}")
    print(f"  Avg ease factor: {stats['avg_ease_factor']}")

    due = sr.get_due_reviews()
    if due:
        print(f"\nDue Reviews:")
        for memory_id, next_review, interval, ef in due:
            preview = memory_id[:50]
            print(
                f"  {memory_id}: next={next_review}, interval={interval:.1f}d, ef={ef:.1f}"
            )

    sr.close()
