#!/usr/bin/env python3
"""Idempotent backfill script for split-brain database remediation.

Merges missing rows from the divergent source DB (~/.config/agentic-memory/memory/memory.db)
into the primary kernel DB (~/Library/Application Support/AgenticMemory/data/memory.db).

Usage:
    python _scripts/backfill_split_brain.py --dry-run
    python _scripts/backfill_split_brain.py --apply
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from pathlib import Path


def _hash_content(content: str) -> str:
    return hashlib.sha256((content or "").strip().encode("utf-8")).hexdigest()


def backfill(
    src_path: Path,
    dest_path: Path,
    apply_changes: bool = False,
) -> dict:
    if not src_path.exists():
        raise FileNotFoundError(f"Source database not found at {src_path}")
    if not dest_path.exists():
        raise FileNotFoundError(f"Destination database not found at {dest_path}")

    # Read destination existing note IDs and content hashes
    dest_conn = sqlite3.connect(str(dest_path))
    dest_conn.row_factory = sqlite3.Row
    dest_cur = dest_conn.cursor()

    dest_cur.execute("SELECT COUNT(*), MAX(created_at) FROM memories")
    dest_count_before, dest_max_created_before = dest_cur.fetchone()

    dest_cur.execute("SELECT id, content, created_at FROM memories")
    dest_ids = set()
    dest_hashes = set()
    for row in dest_cur.fetchall():
        dest_ids.add(row["id"])
        dest_hashes.add(_hash_content(row["content"]))

    # Read source records
    src_conn = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    src_conn.row_factory = sqlite3.Row
    src_cur = src_conn.cursor()

    src_cur.execute("SELECT COUNT(*), MAX(created_at) FROM memories")
    src_count, src_max_created = src_cur.fetchone()

    # Get column names of memories table
    src_cur.execute("PRAGMA table_info(memories)")
    columns = [col["name"] for col in src_cur.fetchall()]
    placeholders = ",".join(["?"] * len(columns))
    cols_str = ",".join([f'"{c}"' for c in columns])

    src_cur.execute("SELECT * FROM memories ORDER BY created_at ASC")
    src_rows = src_cur.fetchall()

    to_insert = []
    skipped_existing_id = 0
    skipped_existing_hash = 0

    for row in src_rows:
        row_id = row["id"]
        content = row["content"]
        chash = _hash_content(content)

        if row_id in dest_ids:
            skipped_existing_id += 1
            continue
        if chash in dest_hashes:
            skipped_existing_hash += 1
            continue

        values = [row[c] for c in columns]
        to_insert.append(values)

    report = {
        "src_path": str(src_path),
        "dest_path": str(dest_path),
        "src_total_rows": src_count,
        "src_max_created": src_max_created,
        "dest_total_rows_before": dest_count_before,
        "dest_max_created_before": dest_max_created_before,
        "skipped_existing_id": skipped_existing_id,
        "skipped_existing_hash": skipped_existing_hash,
        "rows_to_insert": len(to_insert),
        "applied": apply_changes,
    }

    if apply_changes and to_insert:
        insert_sql = f'INSERT INTO memories ({cols_str}) VALUES ({placeholders})'
        dest_cur.executemany(insert_sql, to_insert)
        dest_conn.commit()

        dest_cur.execute("SELECT COUNT(*), MAX(created_at) FROM memories")
        dest_count_after, dest_max_created_after = dest_cur.fetchone()
        report["dest_total_rows_after"] = dest_count_after
        report["dest_max_created_after"] = dest_max_created_after

    src_conn.close()
    dest_conn.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill split-brain SQLite memories")
    default_src = Path.home() / ".config" / "agentic-memory" / "memory" / "memory.db"
    default_dest = (
        Path.home()
        / "Library"
        / "Application Support"
        / "AgenticMemory"
        / "data"
        / "memory.db"
    )

    parser.add_argument("--src", type=Path, default=default_src, help="Path to source memory.db")
    parser.add_argument("--dest", type=Path, default=default_dest, help="Path to destination memory.db")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Perform dry run without inserting")
    parser.add_argument("--apply", action="store_true", help="Apply the backfill migration")

    args = parser.parse_args()
    apply_flag = bool(args.apply)

    print(f"[*] Checking split-brain databases...")
    print(f"    Source: {args.src}")
    print(f"    Target: {args.dest}")
    print(f"    Mode:   {'APPLY' if apply_flag else 'DRY RUN'}")

    report = backfill(args.src, args.dest, apply_changes=apply_flag)

    print("\n--- Backfill Report ---")
    for k, v in report.items():
        print(f"  {k}: {v}")

    if not apply_flag:
        print("\n[!] Dry run complete. Run with --apply to execute insertion.")
    else:
        print("\n[+] Backfill successfully applied.")


if __name__ == "__main__":
    main()
