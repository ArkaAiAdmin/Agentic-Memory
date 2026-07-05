#!/usr/bin/env python3
"""Phase 3 promotion engine: promote auto-capture drafts to curated tier.

Scans ``memories`` for notes marked ``auto-capture`` at importance ≤ 2,
evaluates retrieval signals (user_access_log counts, KG/backlink presence),
and promotes qualifying notes to importance=4 with ``promoted`` + ``curated``
tags and ``promoted_at`` metadata.

Wiring
------
This cron is designed to run on a schedule (e.g. every 6 h via crontab).
A lightweight promotion scan is also triggered at session-end by the
``memory-session-end`` hook so drafts created during the current session
have a chance to be promoted without waiting for the next cron window.

Promotion policy
----------------
A note is promoted when **≥ 2 retrieval events** are recorded, or when the
note has both KG facts AND semantic/FTS backlinks.  Strict cross-session
lessons (tag ``cross-session``, no ``auto-capture``) are skipped — they are
already at their final tier (importance=2 via cron_cross_session_learn).

Category scope (B2 extension):
  ``category='lessons'`` — high-signal notes auto-routed by the keyword
    heuristic in tool_complete.py (Tier B1).
  ``category='sessions'`` — session transcript notes that also carry the
    ``auto-capture`` tag (manually or otherwise flagged for promotion).
  Both categories are scanned; the ``auto-capture`` tag is the common
  signal that the note has promotion potential.

Usage
-----
    venv/bin/python cron_promote_drafts.py [--db PATH] [--threshold N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _flock import acquire_lock_or_exit  # type: ignore[import]

from infra.infrastructure import resolve_active_memory_dir

_ACQUIRE_WITH_BACKLINKS = 1    # ≥ 1 access + KG/backlinks → eligible
_DEFAULT_THRESHOLD = 2          # minimum retrieval count for promotion

_CATEGORY_FILTER = ("lessons", "sessions")
_MAX_CANDIDATES_PER_RUN = 20


def _get_db_path(cli_override: str | None) -> Path:
    if cli_override:
        return Path(cli_override)
    env = os.environ.get("MEMORY_DB_PATH")
    if env:
        return Path(env)
    return resolve_active_memory_dir() / "memory.db"


def _load_note(conn, note_id):
    row = conn.execute(
        "SELECT id, category, importance, tags, metadata, content "
        "FROM memories WHERE id = ?",
        (note_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "category": row[1],
        "importance": row[2],
        "tags": json.loads(row[3]) if row[3] else [],
        "metadata": json.loads(row[4]) if row[4] else {},
        "content": row[5] or "",
    }


def _retrieval_count(conn: sqlite3.Connection, note_id: str) -> int:
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM user_access_log WHERE note_id = ?",
            (note_id,),
        ).fetchone()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        return 0


def _has_kg_or_backlinks(conn: sqlite3.Connection, note_id: str) -> bool:
    kg = 0
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM kg_facts WHERE source_memory = ?", (note_id,)
        ).fetchone()
        kg = row[0] if row else 0
    except sqlite3.OperationalError:
        pass
    fts = 0
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM memory_chunks WHERE memory_id = ?", (note_id,)
        ).fetchone()
        fts = row[0] if row else 0
    except sqlite3.OperationalError:
        pass
    return (kg > 0) or (fts > 0)


def _is_eligible(note: dict, conn, threshold: int = 2) -> tuple[bool, str]:
    if note.get("category") not in _CATEGORY_FILTER:
        return False, "category_mismatch"
    if note.get("importance", 0) > 2:
        return False, "already_curated"
    tags_lower = [t.lower() for t in note.get("tags", [])]
    if "cross-session" in tags_lower and "auto-capture" not in tags_lower:
        return False, "cross_session_lesson"
    if "auto-capture" not in tags_lower:
        return False, "no_auto_capture_tag"
    if "promoted" in tags_lower:
        return False, "already_promoted"

    retrieval = _retrieval_count(conn, note["id"])
    has_links = _has_kg_or_backlinks(conn, note["id"])

    if retrieval >= threshold:
        return True, f"retrievals={retrieval}"
    if retrieval >= _ACQUIRE_WITH_BACKLINKS and has_links:
        return True, f"retrievals={retrieval}+backlinks"
    return False, f"below_threshold (retrievals={retrieval}, backlinks={has_links})"


def promote_drafts(db_path: Path, threshold: int = 2, dry_run: bool = False) -> dict:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA busy_timeout = 30000;")
    try:
        rows = conn.execute(
            "SELECT id FROM memories "
            "WHERE category IN (?, ?) AND importance <= 2 "
            "AND tags LIKE '%auto-capture%' "
            "AND tags NOT LIKE '%promoted%' "
            "ORDER BY created_at DESC LIMIT ?",
            _CATEGORY_FILTER + (_MAX_CANDIDATES_PER_RUN,),
        ).fetchall()
        candidates = [r[0] for r in rows]

        promoted: list[dict[str, object]] = []
        skipped: list[dict[str, str]] = []

        for note_id in candidates:
            note = _load_note(conn, note_id)
            if note is None:
                skipped.append({"id": note_id, "reason": "not_found"})  # type: ignore[arg-type]
                continue
            ok, reason = _is_eligible(note, conn, threshold=threshold)
            if not ok:
                skipped.append({"id": note_id, "reason": reason})  # type: ignore[arg-type]
                continue

            if dry_run:
                promoted.append({"id": note_id, "reason": reason, "would_promote": True})  # type: ignore[arg-type]
                continue

            meta = note.get("metadata")
            md: dict = dict(meta if isinstance(meta, dict) else {})
            md["promoted_at"] = datetime.now(timezone.utc).isoformat()
            md["promotion_reason"] = reason
            md_json = json.dumps(md, ensure_ascii=False)

            new_tags = list(note.get("tags", []))
            for t in ("promoted", "curated"):
                if t not in new_tags:
                    new_tags.append(t)
            tags_json = json.dumps(new_tags, ensure_ascii=False)

            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE memories SET importance = 4, tags = ?, metadata = ?, "
                "updated_at = ?, superseded_by = NULL WHERE id = ?",
                (tags_json, md_json, now_iso, note_id),
            )
            promoted.append({"id": note_id, "reason": reason})

        conn.commit()
        return {
            "scanned": len(candidates),
            "promoted": promoted,
            "skipped": skipped,
            "dry_run": dry_run,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="cron_promote_drafts")
    parser.add_argument("--db", default=None, help="Override memory.db path")
    parser.add_argument(
        "--threshold",
        type=int,
        default=_DEFAULT_THRESHOLD,
        help="Minimum retrieval count to promote (default: %d)" % _DEFAULT_THRESHOLD,
    )
    parser.add_argument("--dry-run", action="store_true", help="Report only; do not write")
    args = parser.parse_args()

    retrieval_threshold = max(1, args.threshold)

    db_path = _get_db_path(args.db)
    if not db_path.exists():
        print(f"ERROR: no memory.db at {db_path}", file=sys.stderr)
        return 1

    acquire_lock_or_exit("cron_promote_drafts")
    t0 = time.time()
    try:
        result = promote_drafts(db_path, threshold=retrieval_threshold, dry_run=args.dry_run)
        elapsed = time.time() - t0
        n_promoted = len(result["promoted"])
        n_skipped = len(result["skipped"])
        n_scanned = result["scanned"]
        print(
            f"[promote_drafts] scanned={n_scanned} promoted={n_promoted} "
            f"skipped={n_skipped} elapsed={elapsed:.1f}s"
            f"{' (dry run)' if args.dry_run else ''}"
        )
        for rec in result["promoted"]:
            print(f"  promoted: {rec['id']}  reason={rec['reason']}")
        for rec in result["skipped"][:5]:
            print(f"  skipped: {rec['id']}  reason={rec['reason']}")
        return 0
    except Exception as e:
        print(f"[promote_drafts] ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
