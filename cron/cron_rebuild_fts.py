#!/usr/bin/env python3
from __future__ import annotations
"""Cron wrapper: lightweight FTS5 index rebuild.

Runs ``INSERT INTO fts(fts) VALUES('rebuild')`` on every FTS5 virtual
table in the DB. This triggers SQLite's internal B-tree reconstruction
without dropping or re-creating the table — orders of magnitude cheaper
than a full ``rebuild_index.py`` run.

Designed for daily execution as a complement to the heavier weekly
``cron_compact.py`` full rebuild.  Catches and repairs silent B-tree
desynchronization that can accumulate under high write concurrency
(multi-agent environments).

Usage:
    venv/bin/python cron_rebuild_fts.py
"""

from _flock import acquire_lock_or_exit
import os
import sqlite3
import sys
from pathlib import Path

_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)
from infra.infrastructure import resolve_active_memory_dir

from infra.log import setup_logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection


logger = setup_logging("cron_rebuild_fts", level="INFO", fmt="%(asctime)s [%(levelname)s] %(message)s")


def _fts_tables(conn: AnyConnection) -> list[str]:
    """Return names of all FTS5 virtual tables in the database."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%USING fts5%'"
    ).fetchall()
    return [r[0] for r in rows]


def rebuild_all_fts(db_path: Path) -> dict:
    """Run ``INSERT INTO fts(fts) VALUES('rebuild')`` on every FTS5 table.

    Returns a dict with table→rowcount results.
    Uses direct sqlite3.connect (not open_db) to avoid flock conflict
    with the MCP server's write queue.
    """
    results: dict[str, str] = {}
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        tables = _fts_tables(conn)
        if not tables:
            logger.info("No FTS5 tables found — nothing to rebuild")
            return results

        for table in tables:
            try:
                conn.execute(f'INSERT INTO "{table}"("{table}") VALUES(\'rebuild\')')
                conn.commit()
                results[table] = "ok"
                logger.info("Rebuilt FTS5 index for %s", table)
            except sqlite3.Error as exc:
                results[table] = f"error: {exc}"
                logger.warning("FTS5 rebuild failed for %s: %s", table, exc)
    finally:
        conn.close()
    return results


def main() -> int:
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("usage: %s [-h|--help]" % sys.argv[0], file=sys.stderr)
        print("Cron job — runs the scheduled operation; no flags required.", file=sys.stderr)
        sys.exit(0)

    acquire_lock_or_exit('cron_rebuild_fts')

    env = os.environ.get("MEMORY_DB_PATH")
    if env:
        db_path = Path(env)
    else:
        active_dir = resolve_active_memory_dir()
        db_path = active_dir / "memory.db"
    if not db_path.exists():
        logger.error("No memory.db at %s", db_path)
        sys.exit(1)

    logger.info("Rebuilding FTS5 indexes for %s", db_path)
    results = rebuild_all_fts(db_path)
    failed = [t for t, r in results.items() if not r.startswith("ok")]
    if failed:
        for t in failed:
            logger.error("FTS5 rebuild failed: %s -> %s", t, results[t])
        sys.exit(1)
    logger.info("Done: %s", results)
    return 0


if __name__ == "__main__":
    main()
