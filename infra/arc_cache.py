#!/usr/bin/env python3
"""Adaptive Replacement Cache ghost-tracking layer for the Agentic Memory system.

ARC (Megiddo & Modha, 2003) splits the cache into two lists (T1, T2) and
two ghost lists (B1, B2). When an eviction turns out to have been a
mistake — i.e. a ghost entry is hit later — ARC adjusts the partition
between T1 and T2 to learn the workload's recency/frequency mix.

In this codebase, ``ARCCache`` only stores the *ghost* side of that
state in SQLite. Real cache evictions happen via
``tier_migration.py`` (which wires into ``ARCCache.record_eviction``
through the post-migration-hook installed on 2026-06-22, P0 fix #4);
this class is the read/write surface for the ghost list and the
eviction-pressure signal that drives the next tier migration.

Public API:
    ARCCache(db_path).record_eviction(id, tier)
    ARCCache(db_path).record_hit(id)
    ARCCache(db_path).record_recent(id)
    ARCCache(db_path).check_ghost(id) -> bool
    ARCCache(db_path).compute_eviction_pressure() -> float
    ARCCache(db_path).cleanup_old_ghosts(max_age_days=30)
    ARCCache(db_path).reset() -> dict
    ARCCache(db_path).get_stats() -> dict

Context manager:
    with ARCCache(db_path) as cache:
        ...

CLI usage:
    python arc_cache.py              # print stats + cleanup, one-shot
"""

from __future__ import annotations

import datetime
import logging
import os
import sqlite3
import sys
from contextlib import contextmanager
from infra.db_write_queue import sqlite_write_queue
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection

from typing import Any, Iterator

from infra.memory_common import find_project_root  # noqa: E402

logger = logging.getLogger(__name__)


# M8 fix: ARC ghost table schema. Kept here so the module is the single
# source of truth for the on-disk shape; tier_migration.py reads the same
# columns by name.
#
# 2026-06-22 (D2 fix): the canonical schema for these tables is now in
# `migrations/014_arc_cache.sql`. The `CREATE TABLE IF NOT EXISTS`
# statements below are a SAFETY NET for callers that open `ARCCache`
# against an un-migrated DB (e.g. unit tests that create a tempfile
# DB without running `migration_runner`). The two schemas must stay
# in lockstep — see the migration runner for the canonical definition
# and the audit comment at the top of this module for the reason the
# safety net exists at all.
_ARC_SCHEMA = """
CREATE TABLE IF NOT EXISTS arc_ghosts (
    memory_id TEXT PRIMARY KEY,
    evicted_at TEXT NOT NULL,
    tier TEXT NOT NULL,
    would_have_been_hit INTEGER DEFAULT 0
)
"""

_ARC_STATS_SCHEMA = """
CREATE TABLE IF NOT EXISTS arc_stats (
    key TEXT PRIMARY KEY,
    value REAL DEFAULT 0.0
)
"""


class ARCCache:
    """SQLite-backed ghost list + eviction-pressure signal.

    The cache is opened with a 30 s busy timeout so concurrent writers
    (e.g. tier_migration rebuilding the live memory DB) don't cause
    immediate failures on a busy ghost table.

    Args:
        db_path: Filesystem path to a SQLite database file. The file is
            created if it does not exist.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db: AnyConnection = sqlite_write_queue.start_session(self.db_path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout = 30000;")
        self._ensure_tables()

    def __enter__(self) -> "ARCCache":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """Yield a cursor inside an explicit transaction.

        Commits on success, rolls back on any exception. The connection
        is left usable after a rollback.
        """
        cur = self.db.cursor()
        try:
            yield cur
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        finally:
            cur.close()

    def _ensure_tables(self) -> None:
        with self.transaction() as cur:
            cur.execute(_ARC_SCHEMA)
            cur.execute(_ARC_STATS_SCHEMA)

    def record_eviction(self, memory_id: str, tier: str) -> None:
        """Record that ``memory_id`` was evicted from ``tier``.

        Idempotent: calling twice with the same ``memory_id`` updates the
        ``evicted_at`` timestamp and tier to the latest values.

        P0 fix #4: also writes ``last_eviction_at`` and increments
        ``eviction_total`` in arc_stats so the MCP tool can report a
        real "last eviction" timestamp and lifetime count.
        """
        now_ts = datetime.datetime.now().timestamp()
        with self.transaction() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO arc_ghosts
                       (memory_id, evicted_at, tier, would_have_been_hit)
                   VALUES (?, ?, ?, 0)""",
                (
                    memory_id,
                    datetime.datetime.now().isoformat(),
                    tier,
                ),
            )
            cur.execute(
                """INSERT OR REPLACE INTO arc_stats (key, value)
                   VALUES ('last_eviction_at', ?)""",
                (now_ts,),
            )
            cur.execute(
                """UPDATE arc_stats
                      SET value = COALESCE(value, 0.0) + 1
                    WHERE key = 'eviction_total'"""
            )
            if cur.rowcount == 0:
                cur.execute(
                    """INSERT OR IGNORE INTO arc_stats (key, value)
                       VALUES ('eviction_total', 1.0)"""
                )

    def record_hit(self, memory_id: str) -> None:
        """Mark a ghost entry as a would-have-been hit.

        The next call to ``compute_eviction_pressure`` will fold this
        signal into the global hit rate. If ``memory_id`` is not in the
        ghost list, this is a silent no-op (the eviction happened
        before the ghost list was created or after cleanup).
        """
        with self.transaction() as cur:
            cur.execute(
                """UPDATE arc_ghosts
                      SET would_have_been_hit = 1
                    WHERE memory_id = ?""",
                (memory_id,),
            )

    def record_recent(self, memory_id: str) -> None:
        """Mark ``memory_id`` as freshly used (cache-hit on T1/T2 side).

        P0 fix #4: called by the embedding-search hot path every time a
        memory comes back from search. Writes the timestamp to arc_stats
        so memory_arc_stats can show "last_recent_at" without recomputing,
        and bumps a per-memory counter to support future per-key stats.

        Best-effort: any DB failure is swallowed because the live search
        path must never block on telemetry.
        """
        try:
            now_ts = datetime.datetime.now().timestamp()
            with self.transaction() as cur:
                cur.execute(
                    """INSERT OR REPLACE INTO arc_stats (key, value)
                       VALUES ('last_recent_at', ?)""",
                    (now_ts,),
                )
                cur.execute(
                    """UPDATE arc_stats
                          SET value = COALESCE(value, 0.0) + 1
                        WHERE key = 'recent_total'"""
                )
                if cur.rowcount == 0:
                    cur.execute(
                        """INSERT OR IGNORE INTO arc_stats (key, value)
                           VALUES ('recent_total', 1.0)"""
                    )
                cur.execute(
                    """INSERT OR REPLACE INTO arc_stats (key, value)
                       VALUES (?, ?)""",
                    (f"recent:{memory_id}", now_ts),
                )
        except Exception as e:
            logger.warning("ARCCache.record_recent failed for %s: %s", memory_id, e)

    def reset(self) -> dict:
        """Clear all ARC state — both the ghost list and the stats table.

        P0 fix #4: backing the new ``memory_arc_reset`` MCP tool. Returns
        a small dict with the row counts that were deleted, so the caller
        can report what was actually wiped.
        """
        with self.transaction() as cur:
            ghosts_deleted = cur.execute("DELETE FROM arc_ghosts").rowcount
            stats_deleted = cur.execute("DELETE FROM arc_stats").rowcount
        return {
            "ghosts_deleted": int(ghosts_deleted or 0),
            "stats_deleted": int(stats_deleted or 0),
        }

    def check_ghost(self, memory_id: str) -> bool:
        """Return True if ``memory_id`` is a ghost that would have been a hit.

        Ghost entries that were never hit return False. Unknown memory
        ids return False (no ghost exists).
        """
        row = self.db.execute(
            """SELECT would_have_been_hit FROM arc_ghosts
                WHERE memory_id = ?""",
            (memory_id,),
        ).fetchone()
        return row is not None and row[0] == 1

    def compute_eviction_pressure(self) -> float:
        """Compute an eviction-pressure signal in [0, 1].

        pressure = 1 - (ghost_hit_rate)
            * 1.0  -> evict aggressively (ghosts were never useful)
            * 0.0  -> stop evicting (ghosts were all would-have-been hits)

        A neutral default of 0.5 is returned when there are no ghost
        entries, so a fresh install behaves the same as a perfectly
        balanced workload.

        The computed pressure plus the underlying hit rate and ghost
        count are also written to the ``arc_stats`` table so the CLI
        and dashboards can read them without recomputing.
        """
        with self.transaction() as cur:
            total_ghosts = cur.execute("SELECT COUNT(*) FROM arc_ghosts").fetchone()[0]
            if total_ghosts == 0:
                pressure = 0.5
            else:
                hits = cur.execute(
                    """SELECT COUNT(*) FROM arc_ghosts
                        WHERE would_have_been_hit = 1"""
                ).fetchone()[0]
                hit_rate = hits / total_ghosts
                pressure = 1.0 - hit_rate
            cur.execute(
                """INSERT OR REPLACE INTO arc_stats (key, value)
                       VALUES ('eviction_pressure', ?),
                              ('ghost_hit_rate',     ?),
                              ('total_ghosts',       ?)""",
                (pressure, 1.0 - pressure, float(total_ghosts)),
            )
        return pressure

    def cleanup_old_ghosts(self, max_age_days: int = 30) -> int:
        """Delete ghost entries older than ``max_age_days``.

        Returns the number of rows deleted. Safe to call on a freshly
        initialized cache (deletes 0 rows).
        """
        cutoff = (
            datetime.datetime.now() - datetime.timedelta(days=max_age_days)
        ).isoformat()
        with self.transaction() as cur:
            cur.execute("DELETE FROM arc_ghosts WHERE evicted_at < ?", (cutoff,))
            return cur.rowcount or 0

    def get_stats(self) -> dict:
        """Return a dict of ARC stats: eviction_pressure, ghost_hit_rate,
        total_ghosts, ghost_count, recent_total, last_recent_at,
        last_eviction_at. The latter is duplicated with total_ghosts for
        backwards compatibility with the original standalone-CLI print
        format.
        """
        stats: dict = {}
        for row in self.db.execute("SELECT key, value FROM arc_stats").fetchall():
            stats[row[0]] = row[1]
        ghost_count_row = self.db.execute(
            "SELECT COUNT(*) FROM arc_ghosts"
        ).fetchone()
        stats["ghost_count"] = ghost_count_row[0] if ghost_count_row is not None else 0
        return stats

    def close(self) -> None:
        """Close the underlying connection. Idempotent."""
        try:
            self.db.close()
        except Exception as e:
            logger.warning("ARCCache.close failed: %s", e)


if __name__ == "__main__":
    db_env = os.environ.get("MEMORY_DB_PATH")
    if db_env:
        db_path = Path(db_env)
    else:
        root = find_project_root(Path.cwd())
        if root is None:
            print("Error: Could not find project root.")
            sys.exit(1)
        db_path = root / "memory" / "memory.db"

    with ARCCache(db_path) as cache:
        stats = cache.get_stats()
        print("ARC Cache Stats:")
        print(f"  Ghost entries:      {stats.get('ghost_count', 0)}")
        print(f"  Eviction pressure:  {stats.get('eviction_pressure', 0.5):.2f}")
        print(f"  Ghost hit rate:     {stats.get('ghost_hit_rate', 0.0):.2%}")
        if "recent_total" in stats:
            print(f"  Recent lookups:     {int(stats['recent_total'])}")
        if "eviction_total" in stats:
            print(f"  Lifetime evictions: {int(stats['eviction_total'])}")
        deleted = cache.cleanup_old_ghosts()
        if deleted:
            print(f"  Pruned {deleted} old ghost entries.")
