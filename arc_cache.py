#!/usr/bin/env python3
import sys
import sqlite3
import datetime
from pathlib import Path

def find_project_root(start_path):
    for path in [start_path] + list(start_path.parents):
        if (path / 'memory').is_dir() or (path / '.git').exists() or (path / 'CLAUDE.md').exists():
            return path
    return start_path

class ARCCache:
    def __init__(self, db_path):
        self.db = sqlite3.connect(str(db_path))
        self.db.execute("PRAGMA busy_timeout = 30000;")
        self._ensure_tables()

    def _ensure_tables(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS arc_ghosts (
                memory_id TEXT PRIMARY KEY,
                evicted_at TEXT NOT NULL,
                tier TEXT NOT NULL,
                would_have_been_hit INTEGER DEFAULT 0
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS arc_stats (
                key TEXT PRIMARY KEY,
                value REAL DEFAULT 0.0
            )
        """)
        self.db.commit()

    def record_eviction(self, memory_id, tier):
        """Record that a memory was evicted from a tier."""
        self.db.execute("""
            INSERT OR REPLACE INTO arc_ghosts (memory_id, evicted_at, tier)
            VALUES (?, ?, ?)
        """, (memory_id, datetime.datetime.now().isoformat(), tier))
        self.db.commit()

    def record_hit(self, memory_id):
        """Record that a ghost entry was requested (would-have-been hit)."""
        self.db.execute("""
            UPDATE arc_ghosts SET would_have_been_hit = 1
            WHERE memory_id = ?
        """, (memory_id,))
        self.db.commit()

    def check_ghost(self, memory_id):
        """Check if a memory is a ghost entry. Returns True if it was evicted and would have been a hit."""
        row = self.db.execute("""
            SELECT would_have_been_hit FROM arc_ghosts
            WHERE memory_id = ?
        """, (memory_id,)).fetchone()
        return row is not None and row[0] == 1

    def compute_eviction_pressure(self):
        """Compute how aggressive eviction should be based on ghost hit rate."""
        total_ghosts = self.db.execute("SELECT COUNT(*) FROM arc_ghosts").fetchone()[0]
        if total_ghosts == 0:
            return 0.5  # Default pressure

        hits = self.db.execute("SELECT COUNT(*) FROM arc_ghosts WHERE would_have_been_hit = 1").fetchone()[0]
        hit_rate = hits / total_ghosts

        # High hit rate = eviction too aggressive = reduce pressure
        # Low hit rate = eviction too lazy = increase pressure
        pressure = 1.0 - hit_rate  # Invert: high hits = low pressure

        # Store stats
        self.db.execute("INSERT OR REPLACE INTO arc_stats (key, value) VALUES (?, ?)",
                        ('eviction_pressure', pressure))
        self.db.execute("INSERT OR REPLACE INTO arc_stats (key, value) VALUES (?, ?)",
                        ('ghost_hit_rate', hit_rate))
        self.db.execute("INSERT OR REPLACE INTO arc_stats (key, value) VALUES (?, ?)",
                        ('total_ghosts', total_ghosts))
        self.db.commit()

        return pressure

    def cleanup_old_ghosts(self, max_age_days=30):
        """Remove ghost entries older than max_age_days."""
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=max_age_days)).isoformat()
        self.db.execute("DELETE FROM arc_ghosts WHERE evicted_at < ?", (cutoff,))
        self.db.commit()

    def get_stats(self):
        """Get current ARC statistics."""
        stats = {}
        for row in self.db.execute("SELECT key, value FROM arc_stats").fetchall():
            stats[row[0]] = row[1]
        stats['ghost_count'] = self.db.execute("SELECT COUNT(*) FROM arc_ghosts").fetchone()[0]
        return stats

    def close(self):
        self.db.close()

if __name__ == '__main__':
    root = find_project_root(Path.cwd())
    db_path = root / 'memory' / 'memory.db'

    cache = ARCCache(db_path)
    stats = cache.get_stats()
    print(f"ARC Cache Stats:")
    print(f"  Ghost entries: {stats.get('ghost_count', 0)}")
    print(f"  Eviction pressure: {stats.get('eviction_pressure', 0.5):.2f}")
    print(f"  Ghost hit rate: {stats.get('ghost_hit_rate', 0.0):.2%}")

    # Cleanup old ghosts
    cache.cleanup_old_ghosts()
    cache.close()
