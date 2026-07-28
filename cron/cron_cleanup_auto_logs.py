#!/usr/bin/env python3
"""Cron wrapper: archive auto-save tool-log entries older than 30 days.

Moves sessions/auto-*.md files older than AUTO_SAVE_CLEANUP_AGE_DAYS
(default 30) from memory/sessions/ to memory/log-archive/ and soft-deletes
the matching rows in the memories table.

Respects flock: two instances never run concurrently.
"""

from __future__ import annotations

import logging
logger = logging.getLogger(__name__)

import json
import os
import sys
import datetime
import re
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

DEFAULT_MAX_AGE_DAYS = 30


def _parse_auto_ts(path: Path, default_tz: datetime.timezone | None = None) -> float | None:
    """Extract UTC timestamp from auto-*.md filename, or None if unparseable.

    The filename embeds the original creation timestamp with an optional
    timezone offset (e.g. +00-00 for UTC, +05-30 for IST).  We parse
    the offset when present so the age calculation is correct regardless
    of the creator's timezone.  When the offset is absent (legacy
    filenames), ``default_tz`` is used; if that is also None we fall
    back to UTC so the comparison against the UTC-based cutoff stays
    consistent.
    """
    name = path.stem  # "auto-2026-06-15_19-29-44+00-00-bash"
    parts = name.split("-", 1)
    if len(parts) < 2:
        return None
    rest = parts[1]  # "2026-06-15_19-29-44+00-00-bash"
    # Split off the tool slug first so we can see the full datetime+tz
    tool_slug = rest.rsplit("-", 1)[-1]  # "bash"
    datetime_part = rest[: -len(tool_slug) - 1]  # "2026-06-15_19-29-44+00-00"
    # Extract timezone offset if present: +/-HH-MM
    tz_match = re.search(r"([+\-]\d{2})-(\d{2})$", datetime_part)
    tz_offset = None
    if tz_match:
        tz_hours = int(tz_match.group(1)[1:])
        if tz_hours <= 23:  # valid timezone offset (max +23:00)
            tz_sign = 1 if tz_match.group(1)[0] == "+" else -1
            tz_minutes = int(tz_match.group(2))
            tz_offset = datetime.timezone(
                datetime.timedelta(hours=tz_sign * tz_hours, minutes=tz_sign * tz_minutes)
            )
            datetime_part = datetime_part[:tz_match.start()]
    # Replace dashes in time portion: YYYY-MM-DD_HH-MM-SS -> YYYY-MM-DD HH:MM:SS
    try:
        date_part, time_part = datetime_part.split("_", 1)
        time_clean = time_part.replace("-", ":")
        dt = datetime.datetime.strptime(
            f"{date_part} {time_clean}", "%Y-%m-%d %H:%M:%S"
        )
        if tz_offset is not None:
            dt = dt.replace(tzinfo=tz_offset)
        elif default_tz is not None:
            dt = dt.replace(tzinfo=default_tz)
        else:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except Exception as e:
        logger.warning("_parse_auto_ts failed: %s", e)
        return None


def cleanup_auto_logs(
    dry_run: bool = False,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    default_tz: datetime.timezone | None = None,
) -> dict:
    db_path = get_db_path()
    if not db_path.exists():
        return {"error": "no database found", "moved": 0}

    sessions_dir = _get_sessions_dir()
    archive_dir = _auto_log_archive_dir()
    cutoff = time.time() - max_age_days * 86400

    candidates = sorted(sessions_dir.glob("auto-*.md"))
    to_move = []
    for path in candidates:
        ts = _parse_auto_ts(path, default_tz=default_tz)
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
    import sqlite3
    db = sqlite3.connect(str(db_path), timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
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
                logger.warning("cleanup_auto_logs failed: %s", exc)
                errors.append(f"{path.name}: {exc}")
        db.commit()
    except Exception as e:
        logger.warning("cleanup_auto_logs failed: %s", e)
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
    default_tz = None
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--max-age-days" and i < len(sys.argv):
            try:
                max_age = int(sys.argv[i + 1])
            except (ValueError, IndexError):
                pass
        elif arg == "--default-tz" and i < len(sys.argv):
            tz_val = sys.argv[i + 1]
            if tz_val.lower() in ("utc", "local"):
                default_tz = datetime.timezone.utc if tz_val.lower() == "utc" else None
            else:
                try:
                    sign = 1 if tz_val[0] == "+" else -1
                    parts = tz_val[1:].split(":")
                    hours = int(parts[0])
                    minutes = int(parts[1]) if len(parts) > 1 else 0
                    default_tz = datetime.timezone(
                        datetime.timedelta(hours=sign * hours, minutes=sign * minutes)
                    )
                except Exception:
                    print(f"WARNING: ignoring invalid --default-tz={tz_val}", file=sys.stderr)

    acquire_lock_or_exit("cron_cleanup_auto_logs")
    try:
        result = cleanup_auto_logs(dry_run=dry_run, max_age_days=max_age, default_tz=default_tz)
        print(json.dumps(result, indent=2))
        if "error" in result:
            sys.exit(1)
    except Exception as e:
        logger.warning("main failed: %s", e)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
