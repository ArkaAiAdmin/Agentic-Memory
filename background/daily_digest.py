#!/usr/bin/env python3
"""Daily digest logic for auto-save.

Extracted from auto_save.py in Phase 3.
"""
from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _build_daily_sections(
    autos: list[Path], date_str: str
) -> tuple[list[str], list[tuple[Path, str, str]], dict[str, int]]:
    from background.auto_save import _truncate  # noqa: E402
    """Walk the auto-save files for ``date_str`` and return:

      * sections: the rendered markdown sections (one per file)
      * path_meta: parallel list of (path, ts_part, tool_slug)
      * tool_counts: tool-slug → count

    The C7 fix lives here: filenames like
    ``auto-{date_str}_{HH-MM-SS}-{tool_slug}.md`` have dashes inside
    the timestamp, so a greedy regex would mis-extract the
    tool_slug. Non-greedy ``(.+?)`` anchored to the last dash before
    ``.md`` is the durable fix.

    Extracted 2026-06-22 from daily_digest().
    """
    from concurrent.futures import ThreadPoolExecutor

    filename_re = re.compile(rf"auto-{re.escape(date_str)}_(.+?)-([^-]+)\.md")

    # First, extract metadata from filenames (fast, no I/O)
    path_meta: list[tuple[Path, str, str]] = []
    tool_counts: dict[str, int] = {}
    for path in autos:
        m = filename_re.match(path.name)
        if m:
            ts_part, tool_slug = m.group(1), m.group(2)
        else:
            ts_part, tool_slug = "unknown", "unknown"
        path_meta.append((path, ts_part, tool_slug))
        tool_counts[tool_slug] = tool_counts.get(tool_slug, 0) + 1

    # Then read file bodies in parallel (I/O bound)
    def read_body(path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="replace")

    with ThreadPoolExecutor(max_workers=4) as executor:
        bodies = list(executor.map(read_body, autos))

    # Build sections from the parallel-read bodies
    sections: list[str] = []
    for (path, ts_part, tool_slug), body in zip(path_meta, bodies):
        ts_match = re.search(r"\*\*Timestamp\*\*: (\S+)", body)
        result_match = re.search(r"## Result \(preview\)\n(.*?)\n---", body, re.DOTALL)
        result_text = result_match.group(1).strip() if result_match else "_no preview_"
        sections.append(
            f"### {ts_part} — `{tool_slug}`\n"
            f"_{ts_match.group(1) if ts_match else ''}_\n\n"
            f"```\n{_truncate(result_text, 200)}\n```"
        )
    return sections, path_meta, tool_counts

def _get_tool_counts_from_db(date_str: str) -> dict[str, int]:
    """Get tool breakdown from database for a given date.

    More efficient than parsing filenames - uses SQL directly on the
    memories table.
    """
    from background.auto_save import get_db_path  # noqa: E402
    try:
        from infra.db import connection_pool

        db_path = get_db_path()
        conn = connection_pool.get(str(db_path), timeout=10.0)
        try:
            # Extract tool slug from source_file which has format:
            # sessions/auto-YYYY-MM-DD_HH-MM-SS-tool_slug.md
            rows = conn.execute(
                """
                SELECT 
                    CASE 
                        WHEN source_file LIKE '%+00-00-%' 
                        THEN substr(source_file, 41, length(source_file) - 43)
                        ELSE substr(source_file, 35, length(source_file) - 37)
                    END as tool_slug,
                    COUNT(*) as cnt
                FROM memories
                WHERE id LIKE 'sessions/auto-%' 
                  AND source_file LIKE 'sessions/auto-' || ? || '_%'
                  AND deleted_at IS NULL
                GROUP BY tool_slug
                """,
                (date_str,),
            ).fetchall()
            return {row[0]: row[1] for row in rows}
        finally:
            from infra.memory_common import safe_close_db

            safe_close_db(conn, should_commit=False)
    except Exception:
        return {}

def _archive_one_autosave(
    path: Path, ts_part: str, tool_slug: str, date_str: str, archive_dir: Path
) -> bool:

    from background.auto_save import get_db_path  # noqa: E402
    """C9 fix: delete the DB row FIRST (idempotent — re-runs are
    safe), then move the file. The previous order (move-then-delete)
    left a window where the file was archived but the DB row leaked.

    Extracted 2026-06-22 from daily_digest().
    """
    note_id = f"sessions/auto-{date_str}_{ts_part}-{tool_slug}"
    try:
        from infra.db_write_queue import sqlite_write_queue
        conn = sqlite_write_queue.start_session(get_db_path())
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            rowid = conn.execute(
                "SELECT rowid FROM memories WHERE id = ?", (note_id,)
            ).fetchone()
            if rowid:
                trigger_exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND name='memories_ad'"
                ).fetchone()
                if not trigger_exists:
                    conn.execute(
                        "DELETE FROM memories_fts WHERE rowid = ?", (rowid[0],)
                    )
            conn.execute("DELETE FROM memories WHERE id = ?", (note_id,))
            fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk_violations:
                logger.warning(
                    "FK violations after daily-digest DELETE: %s",
                    fk_violations[:5],
                )
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        logger.warning("could not delete archived DB row for %s: %s", path.name, e)
    # Now move the file (idempotent: if missing, skip silently).
    try:
        if path.exists():
            shutil.move(str(path), str(archive_dir / path.name))
            return True
    except Exception as e:
        logger.warning("could not move %s: %s", path.name, e)
    return False

def _sweep_orphan_rows() -> None:
    from background.auto_save import get_db_path  # noqa: E402
    """Sweep pre-existing orphan rows in tables that don't have
    ``ON DELETE CASCADE`` or that pre-date ``PRAGMA foreign_keys=ON``.
    Catches rows in user_access_log, memory_embeddings,
    memory_chunks, memory_vec_keys, kg_facts that reference
    deleted memories.

    Extracted 2026-06-22 from daily_digest().
    """
    try:
        from infra.db_write_queue import sqlite_write_queue
        conn = sqlite_write_queue.start_session(get_db_path())
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            try:
                for table, col in [
                    ("user_access_log", "note_id"),
                    ("memory_embeddings", "memory_id"),
                    ("memory_chunks", "parent_id"),
                    ("memory_vec_keys", "memory_id"),
                    ("kg_facts", "source_memory"),
                ]:
                    try:
                        conn.execute(
                            f"DELETE FROM {table} WHERE {col} NOT IN "
                            f"(SELECT id FROM memories)"
                        )
                    except Exception:
                        pass
                conn.commit()
            finally:
                # P0-2 fix (2026-06-24): wrap the restore in try/except so a
                # failure to re-enable foreign_keys doesn't leave the connection
                # with foreign_keys=OFF when it's returned to the pool.
                try:
                    conn.execute("PRAGMA foreign_keys=ON")
                except Exception:
                    logger.warning("Failed to restore PRAGMA foreign_keys=ON")
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        logger.debug("daily-digest orphan cleanup skipped: %s", e)

def daily_digest(date_str: Optional[str] = None, dry_run: bool = False) -> dict:

    from background.auto_save import (
        ARCHIVE_DIR_NAME, _get_sessions_dir, _now_iso, atomic_write,
    )  # noqa: E402
    from background.tool_complete import _upsert_memory  # noqa: E402
    """Roll all auto-*.md notes for `date_str` into one sessions/YYYY-MM-DD.md.

    If date_str is None, defaults to yesterday (most common case: run at
    midnight to roll up the day that's just ended).

    Decomposed 2026-06-22 — 3 named helpers handle the section
    building, per-file archive move, and orphan sweep. The
    orchestrator below reads as a 5-step pipeline.
    """
    if date_str is None:
        date_str = (datetime.today() - timedelta(days=1)).isoformat()
    # Validate
    try:
        datetime.fromisoformat(date_str)
    except ValueError:
        return {"digested": 0, "error": f"invalid date: {date_str}"}

    _get_sessions_dir().mkdir(parents=True, exist_ok=True)
    target_note_id = f"sessions/{date_str}"
    target_path = _get_sessions_dir() / f"{date_str}.md"

    # Find auto-saves for this date
    prefix = f"auto-{date_str}_"
    autos = sorted(_get_sessions_dir().glob(f"{prefix}*.md"))
    if not autos:
        return {"digested": 0, "date": date_str, "note": "no auto-saves found"}

    sections, path_meta, _ = _build_daily_sections(autos, date_str)

    # Use SQL for tool breakdown (more efficient than file parsing)
    tool_counts = _get_tool_counts_from_db(date_str)
    if not tool_counts:
        # Fallback to file parsing if SQL query returns empty
        _, _, tool_counts = _build_daily_sections(autos, date_str)

    tool_summary = ", ".join(
        f"`{k}`×{v}" for k, v in sorted(tool_counts.items(), key=lambda x: -x[1])
    )
    ts = _now_iso()
    daily_md = f"""---
created: {ts}
updated: {ts}
observed_at: {ts}
tags: [daily-digest, session-log, {date_str}]
pinned: false
related: []
valid_from: {ts}
valid_to: null
superseded_by: null
---

# Session Digest: {date_str}

**Auto-saves captured**: {len(autos)}
**Tool breakdown**: {tool_summary or "_none_"}

## Timeline

{chr(10).join(sections)}

---
*Auto-generated by auto_save.py daily-digest. Source auto-saves moved to `sessions/archive/auto-{date_str}/`.*
"""
    if dry_run:
        return {"digested": len(autos), "date": date_str, "preview": daily_md[:500]}

    atomic_write(target_path, daily_md, encoding="utf-8")

    archive_dir = _get_sessions_dir() / ARCHIVE_DIR_NAME / f"auto-{date_str}"
    archive_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for path, ts_part, tool_slug in path_meta:
        if _archive_one_autosave(path, ts_part, tool_slug, date_str, archive_dir):
            moved += 1

    _sweep_orphan_rows()

    _upsert_memory(
        target_note_id,
        f"sessions/{date_str}.md",
        daily_md,
        ["daily-digest", "session-log", date_str],
        ts,
        pinned=0,
        importance=2,
    )
    return {
        "digested": moved,
        "date": date_str,
        "note_id": target_note_id,
        "tool_breakdown": tool_counts,
    }
