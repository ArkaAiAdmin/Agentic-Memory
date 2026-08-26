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

    Previously only handled ``sessions/auto-%`` (kernel hook). Now also covers
    ``auto_save`` category (IDE executor: 3382/5211 active = 65% bloat, 2504 <300 chars).
    Queries for ``(id LIKE 'sessions/auto-%' OR category='auto_save')`` and
    soft-deletes them, moving markdown files to their respective
    ``archive/purged-{date}/`` dirs.

    This is a one-shot cleanup for the 3412+ zero-importance auto-save
    entries that accumulated before the allow-list was introduced, plus the
    IDE double-save bloat (tool-intent + tool-result per invocation).

    Returns a dict with counts of deleted DB rows and moved files.
    """
    from background.auto_save import get_db_path, _get_sessions_dir, _get_memory_dir, _now_iso  # noqa: E402
    from infra._lazy_imports import connection_pool

    db_path = get_db_path()
    if not db_path.exists():
        return {"error": "no database found", "deleted": 0}

    db = connection_pool.get(str(db_path), timeout=30.0)
    db.row_factory = sqlite3.Row
    try:
        # Cover both kernel sessions/auto-% and IDE auto_save/* (fix for 65% bloat)
        rows = db.execute(
            "SELECT id, source_file, category FROM memories "
            "WHERE (id LIKE 'sessions/auto-%' OR category='auto_save') AND deleted_at IS NULL"
        ).fetchall()
        note_ids = [r["id"] for r in rows]
        # Keep category for file path resolution
        row_infos = [(r["id"], r["source_file"], r["category"]) for r in rows]

        # Dry-run: report would-delete plus orphan estimate
        if dry_run:
            sessions_cnt = sum(1 for _, _, c in row_infos if c == "sessions")
            autosave_cnt = sum(1 for _, _, c in row_infos if c == "auto_save")
            # Estimate orphan files on disk not in DB
            orphan_estimate = 0
            try:
                from pathlib import Path as _P

                _mem_dir = _get_memory_dir()
                _as_dir = _mem_dir / "auto_save"
                if _as_dir.exists():
                    _db_fnames = set()
                    for _nid, _sf, _ in row_infos:
                        _b = _P(_sf).name if _sf else _P(_nid.split("/", 1)[-1]).name
                        if not _b.endswith(".md"):
                            _b += ".md"
                        _db_fnames.add(_b)
                    orphan_estimate = sum(
                        1 for _f in _as_dir.glob("*.md") if _f.name not in _db_fnames
                    )
            except Exception:
                pass
            return {
                "dry_run": True,
                "would_delete": len(note_ids),
                "would_delete_sessions": sessions_cnt,
                "would_delete_auto_save": autosave_cnt,
                "orphan_files": orphan_estimate,
                "sample_ids": note_ids[:5],
            }

        if not note_ids:
            # No DB rows but still sweep orphan files on disk
            try:
                _mem_dir2 = _get_memory_dir()
                _as_dir2 = _mem_dir2 / "auto_save"
                if _as_dir2.exists() and any(_as_dir2.glob("*.md")):
                    pass  # fall through to orphan sweep below
                else:
                    return {"deleted": 0, "message": "no auto-save entries found"}
            except Exception:
                return {"deleted": 0, "message": "no auto-save entries found"}

        now_ts = _now_iso()
        db.executemany(
            "UPDATE memories SET deleted_at = ? WHERE id = ?",
            [(now_ts, nid) for nid in note_ids],
        )
        db.commit()

        sessions_dir = _get_sessions_dir()
        memory_dir = _get_memory_dir()
        archive_name = f"purged-{datetime.date.today().isoformat()}"

        moved = 0
        # Also handle orphan files on disk not in DB (7352 files observed in auto_save dir)
        # We move DB-referenced files first, then sweep orphans.
        for nid, sf, cat in row_infos:
            try:
                if cat == "sessions":
                    archive_dir = sessions_dir / "archive" / archive_name
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    # source_file may be just filename or with dir
                    fname = Path(sf).name if sf else Path(nid).name + ".md"
                    if not fname.endswith(".md"):
                        fname += ".md"
                    src = sessions_dir / fname
                    if src.exists():
                        shutil.move(str(src), str(archive_dir / src.name))
                        moved += 1
                elif cat == "auto_save":
                    auto_save_dir = memory_dir / "auto_save"
                    archive_dir = auto_save_dir / "archive" / archive_name
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    fname = Path(sf).name if sf else Path(nid.split("/", 1)[-1]).name
                    if not fname.endswith(".md"):
                        fname += ".md"
                    src = auto_save_dir / fname
                    # Fallback: try memory_dir / source_file if not in auto_save subdir
                    if not src.exists() and sf:
                        alt = memory_dir / sf
                        if alt.exists():
                            src = alt
                    if src.exists():
                        shutil.move(str(src), str(archive_dir / src.name))
                        moved += 1
                else:
                    # Generic fallback for unexpected categories
                    archive_dir = memory_dir / cat / "archive" / archive_name
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    fname = Path(sf).name if sf else Path(nid.split("/", 1)[-1]).name
                    src = memory_dir / cat / fname
                    if src.exists():
                        shutil.move(str(src), str(archive_dir / src.name))
                        moved += 1
            except Exception as e:
                logger.warning("purge move failed for %s: %s", nid, e)

        # Sweep orphan auto_save files not in DB (disk bloat: 7352 files vs 3382 DB rows,
        # plus legacy about-to-execute-*.md naming from IDE executor pre-filter)
        try:
            auto_save_dir = memory_dir / "auto_save"
            if auto_save_dir.exists():
                # Build set of filenames that were in DB (already moved) — normalize .md suffix
                db_fnames = set()
                for nid, sf, _ in row_infos:
                    base = Path(sf).name if sf else Path(nid.split("/", 1)[-1]).name
                    if not base.endswith(".md"):
                        base += ".md"
                    db_fnames.add(base)
                for f in auto_save_dir.glob("*.md"):
                    if f.name not in db_fnames and f.is_file():
                        # Orphan: move to same archive (covers auto-*.md and about-to-execute-*.md)
                        archive_dir = auto_save_dir / "archive" / archive_name
                        archive_dir.mkdir(parents=True, exist_ok=True)
                        try:
                            shutil.move(str(f), str(archive_dir / f.name))
                            moved += 1
                        except Exception as e:
                            logger.warning("orphan purge failed for %s: %s", f, e)
        except Exception as e:
            logger.warning("orphan sweep failed: %s", e)

        return {
            "deleted": len(note_ids),
            "files_moved": moved,
            "archive_dir": str(sessions_dir / "archive" / archive_name),
            "auto_save_archive_dir": str((memory_dir / "auto_save" / "archive" / archive_name)),
        }
    finally:
        try:
            connection_pool.put(db)
        except Exception:
            pass
