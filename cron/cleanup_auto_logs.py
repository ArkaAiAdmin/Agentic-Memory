#!/usr/bin/env python3
"""Cron wrapper: archive auto-save tool-log entries older than 30 days.

Moves sessions/auto-*.md files older than AUTO_SAVE_CLEANUP_AGE_DAYS
(default 30) from memory/sessions/ to memory/log-archive/ and soft-deletes
the matching rows in the memories table.

Respects flock: two instances never run concurrently.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

# Bootstrap sys.path so we can import project modules.
_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)

from _flock import acquire_lock_or_exit  # noqa: E402
from background.auto_save import _get_sessions_dir, _auto_log_archive_dir, get_db_path  # noqa: E402
from infra.db_write_queue import sqlite_write_queue  # noqa: E402

DEFAULT_MAX_AGE_DAYS = 30


def _parse_auto_ts(path: Path):
    """Extract datetime from auto-*.md filename, or None if unparseable."""
    name = path.stem  # "auto-2026-06-15_19-29-44+00-00-bash"
    parts = name.split("-", 1)
    if len(parts) < 2:
        return None
    rest = parts[1]  # "2026-06-15_19-29-44+00-00-bash"
    date_time = rest.rsplit("-", 1)[0]  # "2026-06-15_19-29-44+00-00"
    # Replace dashes in time portion to recover ISO-ish format
    # Format: YYYY-MM-DD_HH-MM-SS[+HH-MM]
    try:
        date_part, time_part = date_time.split("_", 1)
        time_clean = time_part.replace("-", ":")
        # Strip tz offset for fromisoformat (Python 3.7+)
        tz_pos = time_clean.find("+")
        if tz_pos != -1:
            time_clean = time_clean[:tz_pos]
        return time.mktime(
            time.strptime(f"{date_part} {time_clean}", "%Y-%m-%d %H:%M:%S")
        )
    except Exception:
        return None


def cleanup_auto_logs(dry_run: bool = False, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> dict:
    db_path = get_db_path()
    if not db_path.exists():
        return {"error": "no database found", "moved": 0}

    sessions_dir = _get_sessions_dir()
    archive_dir = _auto_log_archive_dir()
    cutoff = time.time() - max_age_days * 86400

    candidates = sorted(sessions_dir.glob("auto-*.md"))
    to_move = []
    for path in candidates:
        ts = _parse_auto_ts(path)
        if ts is None or ts < cutoff:
            to_move.append(path)

    if not to_move:
        return {"moved": 0, "message": "no expired auto-save files found"}

    if dry_run:
        return {
            "dry_run": True,
            "would_move": len(to_move),
            "cutoff_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff)),
            "sample": [str(p.name) for p in to_move[:5]],
        }

    archive_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite_write_queue.start_session(db_path)
    moved = 0
    errors = []
    try:
        for path in to_move:
            note_id = f"sessions/{path.name}"
            try:
                db.execute(
                    "UPDATE memories SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                    (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), note_id),
                )
                dest = archive_dir / path.name
                if dest.exists():
                    dest = dest.with_suffix(f".{int(time.time())}{path.suffix}")
                path.rename(dest)
                moved += 1
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    return {
        "moved": moved,
        "errors": errors,
        "archive_dir": str(archive_dir),
        "cutoff_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff)),
    }


def main() -> int:
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print(
            "usage: %s [-h|--help] [--dry-run] [--max-age-days N]" % sys.argv[0],
            file=sys.stderr,
        )
        print(
            "Cron job — archives auto-save tool-log entries older than %d days."
            % DEFAULT_MAX_AGE_DAYS,
            file=sys.stderr,
        )
        sys.exit(0)

    dry_run = "--dry-run" in sys.argv[1:]
    max_age = DEFAULT_MAX_AGE_DAYS
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--max-age-days" and i < len(sys.argv):
            try:
                max_age = int(sys.argv[i + 1])
            except (ValueError, IndexError):
                pass

    acquire_lock_or_exit("cron_cleanup_auto_logs")
    try:
        result = cleanup_auto_logs(dry_run=dry_run, max_age_days=max_age)
        print(json.dumps(result, indent=2))
        if "error" in result:
            sys.exit(1)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
