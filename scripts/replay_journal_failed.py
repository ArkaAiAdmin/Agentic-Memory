#!/usr/bin/env python3
"""Replay dead-lettered journal entries back through the sanctioned write path.

The 2026-08-22 App Support storage migration left category dirs symlinked
across two roots; the traversal guard misread the resolution as an escape
and dead-lettered 1,318 real writes into ``journal_failed``. The guard is
fixed (save/pipeline.py ``_category_allowed_bases``); this tool recovers
the preserved payloads by re-enqueueing them via ``enqueue_write`` — the
same Rule-1-sanctioned journal entry point as any live save.

Safety model:
  - dry-run by default; ``--apply`` required to mutate anything
  - every processed row is first archived verbatim to a JSONL backup file
  - duplicates (note_id already materialized) are skipped, not re-written
  - only TRAVERSAL failures are replayed by default; pass --all to widen
  - rows that fail to re-enqueue stay in journal_failed untouched

Usage:
  venv/bin/python scripts/replay_journal_failed.py --db "<journal.db>"           # preview
  venv/bin/python scripts/replay_journal_failed.py --db "<journal.db>" --apply   # execute
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from infra.write_journal import enqueue_write  # noqa: E402
from save.pipeline import SaveRequest  # noqa: E402


def _candidate_memory_dbs(journal_path: Path) -> list[Path]:
    """DBs that may already contain materialized copies of a failed entry.

    The drainer writes markdown into the paired root but keeps its DB next
    to wherever its db_path pointed; cover both sides of the shim.
    """
    candidates: list[Path] = []
    seen: set[Path] = set()
    for base in (
        journal_path.resolve().parent,
        INSTALL_DIR / "memory",
    ):
        db = base / "memory.db"
        resolved = db.resolve()
        if db.exists() and resolved not in seen:
            seen.add(resolved)
            candidates.append(db)
    return candidates


def _already_materialized(note_id: str, memory_dbs: list[Path]) -> bool:
    for db in memory_dbs:
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT 1 FROM memories WHERE id = ? LIMIT 1", (note_id,)
                ).fetchone()
            finally:
                conn.close()
            if row:
                return True
        except sqlite3.Error:
            continue
    return False


def _row_to_request(row: sqlite3.Row) -> SaveRequest:
    tags = json.loads(row["tags"]) if row["tags"] else None
    columns = set(row.keys())
    return SaveRequest(
        content=row["content"],
        category=row["category"],
        title_slug=row["title_slug"],
        tags=tags,
        pinned=bool(row["pinned"]),
        is_global=bool(row["is_global"]),
        importance=int(row["importance"]),
        context=row["context"] or "generic",
        defer_expensive=bool(row["defer_expensive"]),
        tenant_id=row["tenant_id"] or "default",
        epistemic_source=row["epistemic_source"] or "agent",
        belief_status=row["belief_status"] or "active",
        asserting_agent_id=(row["asserting_agent_id"] or "") if "asserting_agent_id" in columns else "",
        fact_type=row["fact_type"] or "observation",
        data_subject_sub=row["data_subject_sub"] if "data_subject_sub" in columns else None,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path to journal.db (explicit, per backfill-guard precedent)")
    parser.add_argument("--apply", action="store_true", help="Execute the replay (default: dry-run)")
    parser.add_argument("--all", action="store_true", help="Replay every failure, not just TRAVERSAL ones")
    parser.add_argument("--limit", type=int, default=0, help="Cap rows processed this run (0 = all)")
    args = parser.parse_args()

    journal_path = Path(args.db).expanduser().resolve()
    if not journal_path.exists():
        print(f"error: journal db not found: {journal_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(journal_path))
    conn.row_factory = sqlite3.Row
    where = "" if args.all else "WHERE error LIKE '%TRAVERSAL%'"
    rows = conn.execute(
        f"SELECT * FROM journal_failed {where} ORDER BY original_id"
    ).fetchall()
    conn.close()

    memory_dbs = _candidate_memory_dbs(journal_path)
    backup_dir = journal_path.parent.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_file = backup_dir / f"journal_failed.replay-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.jsonl"

    stats = {"replay": 0, "duplicate": 0, "error": 0}
    processed: list[tuple[sqlite3.Row, str]] = []

    for i, row in enumerate(rows):
        if args.limit and stats["replay"] + stats["duplicate"] >= args.limit:
            break
        note_id = row["note_id"]
        disposition = "replay"
        if _already_materialized(note_id, memory_dbs):
            disposition = "duplicate"
        processed.append((row, disposition))
        if not args.apply:
            stats[disposition] += 1

    if not args.apply:
        print(json.dumps({"mode": "dry-run", "candidates": len(processed), **{
            "would_replay": stats["replay"],
            "duplicate": stats["duplicate"],
        }}, indent=2))
        print("Re-run with --apply to execute. Nothing was written.")
        return 0

    # Archive BEFORE any mutation (Rule 19: data preservation is mandatory).
    with open(backup_file, "a", encoding="utf-8") as fh:
        for row, _disposition in processed:
            fh.write(json.dumps(dict(row), ensure_ascii=False) + "\n")

    del_conn = sqlite3.connect(str(journal_path))
    try:
        for row, disposition in processed:
            note_id = row["note_id"]
            if disposition == "duplicate":
                del_conn.execute("DELETE FROM journal_failed WHERE id = ?", (row["id"],))
                continue
            try:
                # Live drainers/schedulers hold journal write locks in
                # bursts; a plain single attempt loses that race with
                # ``database is locked``. Bounded backoff keeps the tool
                # usable against a running system without masking errors.
                last_exc: Exception | None = None
                for attempt in range(3):
                    try:
                        enqueue_write(journal_path, _row_to_request(row), agent_id=row["agent_id"])
                        last_exc = None
                        break
                    except sqlite3.OperationalError as exc:
                        last_exc = exc
                        if "locked" not in str(exc).lower():
                            raise
                        time.sleep(10 * (attempt + 1))
                if last_exc is not None:
                    raise last_exc
                del_conn.execute("DELETE FROM journal_failed WHERE id = ?", (row["id"],))
                del_conn.commit()
                stats["replay"] += 1
            except Exception as exc:  # noqa: BLE001 — keep replaying other rows
                del_conn.rollback()
                stats["error"] += 1
                print(f"replay failed for {note_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        del_conn.close()

    print(json.dumps({
        "mode": "apply",
        "backup": str(backup_file),
        "replayed": stats["replay"],
        "duplicate": stats["duplicate"],
        "error": stats["error"],
        "remaining_failed": sqlite3.connect(str(journal_path)).execute(
            "SELECT COUNT(*) FROM journal_failed").fetchone()[0],
    }, indent=2))
    return 0 if stats["error"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
