#!/usr/bin/env python3
"""Purge auto-save entries.

Extracted from auto_save.py in Phase 3.
"""
from __future__ import annotations

import datetime
import logging
import shutil
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


def purge_auto_saves(dry_run: bool = False) -> dict:
    """Delete all auto-saved tool-log entries from DB and disk.

    Queries the ``memories`` table for ``note_id LIKE 'sessions/auto-%'``,
    soft-deletes them, and moves the corresponding markdown files to
    ``sessions/archive/purged-{date}/``.

    This is a one-shot cleanup for the 3412+ zero-importance auto-save
    entries that accumulated before the allow-list was introduced.

    Returns a dict with counts of deleted DB rows and moved files.
    """
    from background.auto_save import get_db_path, _get_sessions_dir, _now_iso  # noqa: E402
    from infra.db_write_queue import sqlite_write_queue

    db_path = get_db_path()
    if not db_path.exists():
        return {"error": "no database found", "deleted": 0}

    db = sqlite_write_queue.start_session(db_path)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            "SELECT id, source_file FROM memories WHERE id LIKE 'sessions/auto-%' AND deleted_at IS NULL"
        ).fetchall()
        note_ids = [r["id"] for r in rows]
        source_files = [r["source_file"] for r in rows]

        if not note_ids:
            return {"deleted": 0, "message": "no auto-save entries found"}

        if dry_run:
            return {
                "dry_run": True,
                "would_delete": len(note_ids),
                "sample_ids": note_ids[:5],
            }

        now_ts = _now_iso()
        db.executemany(
            "UPDATE memories SET deleted_at = ? WHERE id = ?",
            [(now_ts, nid) for nid in note_ids],
        )
        db.commit()

        sessions_dir = _get_sessions_dir()
        archive_name = f"purged-{datetime.date.today().isoformat()}"
        archive_dir = sessions_dir / "archive" / archive_name
        archive_dir.mkdir(parents=True, exist_ok=True)

        moved = 0
        for sf in source_files:
            if not sf:
                continue
            src = sessions_dir / Path(sf).name
            if src.exists():
                shutil.move(str(src), str(archive_dir / src.name))
                moved += 1

        return {
            "deleted": len(note_ids),
            "files_moved": moved,
            "archive_dir": str(archive_dir),
        }
    finally:
        db.close()
