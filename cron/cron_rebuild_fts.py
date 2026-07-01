#!/usr/bin/env python3
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
import logging
import os
import sqlite3
import sys
from pathlib import Path

_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)
from infra.memory_common import open_db
from infra.infrastructure import resolve_active_memory_dir

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("cron_rebuild_fts")


def _fts_tables(conn: sqlite3.Connection) -> list[str]:
    """Return names of all FTS5 virtual tables in the database."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%USING fts5%'"
    ).fetchall()
    return [r[0] for r in rows]


def rebuild_all_fts(db_path: Path) -> dict:
    """Run ``INSERT INTO fts(fts) VALUES('rebuild')`` on every FTS5 table.

    Returns a dict with table→rowcount results.
    """
    results: dict[str, str] = {}
    with open_db(db_path, timeout=10.0) as conn:
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
    return results


def main() -> int:
    # argparse handles --help and exits cleanly. The pipeline itself
    # takes no flags.
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("usage: %s [-h|--help]" % sys.argv[0], file=sys.stderr)
        print("Cron job — runs the scheduled operation; no flags required.", file=sys.stderr)
        sys.exit(0)

    env = os.environ.get("MEMORY_DB_PATH")
    if env:
        db_path = Path(env)
    else:
        active_dir = resolve_active_memory_dir()
        db_path = active_dir / "memory.db"
    acquire_lock_or_exit('cron_rebuild_fts')

    if not db_path.exists():
        logger.error("No memory.db at %s", db_path)
        sys.exit(1)

    logger.info("Rebuilding FTS5 indexes for %s", db_path)
    results = rebuild_all_fts(db_path)
    logger.info("Done: %s", results)


if __name__ == "__main__":
    main()
