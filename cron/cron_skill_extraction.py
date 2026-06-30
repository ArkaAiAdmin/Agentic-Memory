#!/usr/bin/env python3
"""Cron wrapper: skill extraction — turn procedural memories into reusable skills.

Scans all live memories, runs is_skill_worthy + extract_skill_from_memory on
each, and persists the results into memory_skills (idempotent — content_hash
deduplication makes re-runs a no-op).

Per H22+ principle: skills are the "shed" — cheap, trigger-token lookups that
bypass the full RAG pipeline. This cron builds the shed.

P0 fix #5: now passes the memory's category column to
``is_skill_worthy`` so the lower-threshold detector can give a
half-signal bias to procedural categories (lessons/, projects/).
Also: a single STRONG procedural signal (code block, numbered
step, or shell command) is now enough to qualify — the previous
2+ signals threshold was too strict and only 1 row ever landed in
memory_skills despite 8,000+ memories.

Usage:
    venv/bin/python cron_skill_extraction.py
    venv/bin/python cron_skill_extraction.py --dry-run
    venv/bin/python cron_skill_extraction.py --since 24h   # only recent memories
"""

from _flock import acquire_lock_or_exit
import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path


_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)


from memory_common import safe_close_db, connection_pool
from infrastructure import resolve_active_memory_dir
from skill_extractor import (
    ensure_skill_schema,
    extract_skill_from_memory,
    save_skill,
    is_skill_worthy,
)


def _existing_skill_hashes(conn: sqlite3.Connection) -> dict:
    """Return {content_hash: (skill_id, updated_at)} for dedup.

    P0 fix #5: access by index so the helper works whether or not the
    connection has row_factory=sqlite3.Row set (e.g. when called from
    the memory_extract_skills MCP tool whose connection comes from
    the pool with no row_factory configured).
    """
    rows = conn.execute(
        "SELECT id, content_hash, updated_at FROM memory_skills WHERE content_hash IS NOT NULL"
    ).fetchall()
    return {r[1]: (r[0], r[2]) for r in rows}


def _memory_updated_since(conn: sqlite3.Connection, since_iso: str) -> list:
    """Return memories updated after `since_iso` (or all if since_iso is empty).

    P0 fix #5: tries to include the ``category`` column for the
    per-category bias in is_skill_worthy; if the column is missing
    (e.g. a pre-migration minimal schema), falls back to a 3-column
    SELECT so the cron still works on older test fixtures.
    """
    has_category = _has_category_column(conn)
    if has_category:
        sel = "id, content, updated_at, category"
    else:
        sel = "id, content, updated_at, '' AS category"
    if since_iso:
        return conn.execute(
            f"SELECT {sel} FROM memories WHERE deleted_at IS NULL AND updated_at >= ?",
            (since_iso,),
        ).fetchall()
    return conn.execute(
        f"SELECT {sel} FROM memories WHERE deleted_at IS NULL"
    ).fetchall()


def _has_category_column(conn: sqlite3.Connection) -> bool:
    """Return True iff the ``memories`` table has a ``category`` column."""
    try:
        rows = conn.execute("PRAGMA table_info(memories)").fetchall()
    except sqlite3.OperationalError:
        return False
    for r in rows:
        name = r["name"] if hasattr(r, "keys") else r[1]
        if name == "category":
            return True
    return False


def run_extraction(
    conn: sqlite3.Connection, since_iso: str = "", dry_run: bool = False
) -> dict:
    """Run skill extraction over memories. Idempotent.

    Counts:
      extracted    — new skills added in this run (content_hash not in DB)
      deduplicated — memories whose content_hash matched an existing skill
      updated      — skills whose content changed (would trigger UPSERT)
      skipped      — memories that didn't qualify as skill-worthy

    P0 fix #5: passes the memory's category to ``is_skill_worthy`` so
    the lower-threshold detector can apply the per-category bias.
    """
    ensure_skill_schema(conn)  # safety net; migration is canonical
    existing = _existing_skill_hashes(conn)
    rows = _memory_updated_since(conn, since_iso)
    if not rows:
        return {
            "scanned": 0,
            "extracted": 0,
            "deduplicated": 0,
            "updated": 0,
            "skipped": 0,
            "dry_run": dry_run,
        }

    scanned = 0
    extracted = 0
    deduplicated = 0
    updated = 0
    skipped = 0
    for r in rows:
        scanned += 1
        # P0 fix #5: access by index so this works whether or not
        # the connection has row_factory=sqlite3.Row set.
        # r = (id, content, updated_at, category)
        rid = r[0]
        content = r[1]
        cat = r[3] or (rid.split("/", 1)[0] if "/" in rid else "")
        if not is_skill_worthy(content, category=cat):
            skipped += 1
            continue
        skill = extract_skill_from_memory(rid, content, category=cat)
        if skill is None:
            skipped += 1
            continue
        if skill["content_hash"] in existing:
            deduplicated += 1
            continue
        if not dry_run:
            save_skill(conn, skill)
        existing[skill["content_hash"]] = (0, None)
        extracted += 1

    if not dry_run:
        conn.commit()
    return {
        "scanned": scanned,
        "extracted": extracted,
        "deduplicated": deduplicated,
        "updated": updated,
        "skipped": skipped,
        "dry_run": dry_run,
    }


def main() -> None:
    os.environ.setdefault("MEMORY_DB_PATH", "")
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(
        description="Extract skills from procedural memories"
    )
    parser.add_argument("--dry-run", action="store_true", help="Don't write to the DB")
    parser.add_argument(
        "--since",
        default="",
        help="Only process memories updated after this ISO timestamp",
    )
    args = parser.parse_args()
    acquire_lock_or_exit('cron_skill_extraction')

    env = os.environ.get("MEMORY_DB_PATH")
    db_path = Path(env) if env else resolve_active_memory_dir() / "memory.db"
    if not db_path.exists():
        print(f"ERROR: no memory.db at {db_path}")
        sys.exit(1)

    conn = connection_pool.get(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    # 2026-06-19 fix: row_factory=sqlite3.Row so that ``r["col"]`` style
    # access in _existing_skill_hashes / _memory_updated_since works.
    # Without this the cron was crashing silently on every Monday 03:45
    # slot (the script was scheduled but never produced output).
    conn.row_factory = sqlite3.Row
    t0 = time.time()
    try:
        result = run_extraction(conn, since_iso=args.since, dry_run=args.dry_run)
        elapsed = time.time() - t0
        print(
            f"Skill extraction complete: scanned={result['scanned']} "
            f"extracted={result['extracted']} updated={result['updated']} "
            f"skipped={result['skipped']} dry_run={result['dry_run']} "
            f"elapsed={elapsed:.1f}s"
        )
    finally:
        safe_close_db(conn)


if __name__ == "__main__":
    main()
