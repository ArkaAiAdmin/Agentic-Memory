#!/usr/bin/env python3
import sys
import json
import sqlite3
import datetime
from pathlib import Path
try:
    import fcntl
except ImportError:
    fcntl = None

def find_project_root(start_path):
    for path in [start_path] + list(start_path.parents):
        if (path / 'memory').is_dir() or (path / '.git').exists() or (path / 'CLAUDE.md').exists():
            return path
    return start_path

def parse_frontmatter(content):
    import re
    content_stripped = content.lstrip()
    match = re.match(r'^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)(.*)', content_stripped, re.DOTALL)
    if not match:
        return {}, content
    yaml_text = match.group(1)
    body = match.group(2)
    metadata = {}
    for line in yaml_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if ':' not in stripped:
            continue
        key, val = stripped.split(':', 1)
        metadata[key.strip()] = val.strip().strip('"').strip("'")
    return metadata, body


class SpacedRepetition:
    def __init__(self, db_path):
        self.db = sqlite3.connect(str(db_path))
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
        row = self.db.execute("SELECT retrieval_count, interval_days, ease_factor FROM review_schedule WHERE memory_id = ?",
                              (memory_id,)).fetchone()

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

        next_review = (datetime.date.today() + datetime.timedelta(days=int(new_interval))).isoformat()
        self.db.execute("""
            INSERT OR REPLACE INTO review_schedule
            (memory_id, retrieval_count, interval_days, next_review, last_reviewed, ease_factor)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (memory_id, new_rc, new_interval, next_review, datetime.date.today().isoformat(), new_ef))
        self.db.commit()

    def record_failure(self, memory_id):
        """Record failed retrieval. Reset interval."""
        self.db.execute("""
            INSERT OR REPLACE INTO review_schedule
            (memory_id, retrieval_count, interval_days, next_review, last_reviewed, ease_factor)
            VALUES (?, 0, 1.0, ?, ?, 2.5)
        """, (memory_id, (datetime.date.today() + datetime.timedelta(days=1)).isoformat(),
               datetime.date.today().isoformat()))
        self.db.commit()

    def get_due_reviews(self, limit=10):
        """Get memories that are due for review."""
        today = datetime.date.today().isoformat()
        return self.db.execute("""
            SELECT memory_id, next_review, interval_days, ease_factor
            FROM review_schedule
            WHERE next_review <= ?
            ORDER BY next_review
            LIMIT ?
        """, (today, limit)).fetchall()

    def get_stats(self):
        """Get review statistics."""
        total = self.db.execute("SELECT COUNT(*) FROM review_schedule").fetchone()[0]
        due = self.db.execute("SELECT COUNT(*) FROM review_schedule WHERE next_review <= ?",
                              (datetime.date.today().isoformat(),)).fetchone()[0]
        avg_ef = self.db.execute("SELECT AVG(ease_factor) FROM review_schedule").fetchone()[0]
        return {
            'total_scheduled': total,
            'due_for_review': due,
            'avg_ease_factor': round(avg_ef, 2) if avg_ef else 2.5
        }

    def close(self):
        self.db.close()


if __name__ == '__main__':
    root = find_project_root(Path.cwd())
    db_path = root / 'memory' / 'memory.db'

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
            print(f"  {memory_id}: next={next_review}, interval={interval:.1f}d, ef={ef:.1f}")

    sr.close()