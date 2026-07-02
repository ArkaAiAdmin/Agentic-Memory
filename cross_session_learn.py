#!/usr/bin/env python3
"""Cron wrapper: cross_session_learn — extract reusable patterns from sessions.

Scans recent session notes, identifies patterns worth saving as
lessons (repeated tool usage, common workflows, solved problems),
and creates lesson notes automatically.

Usage:
    venv/bin/python cross_session_learn.py [--dry-run] [--days N]
"""

import os
import sys
import sqlite3
import logging

logger = logging.getLogger(__name__)
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from infra.infrastructure import resolve_active_memory_dir
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection


# Patterns that suggest a reusable lesson
PATTERN_SIGNALS = [
    # Agent solved a complex problem
    ("solved", "fixed", "resolved", "working now"),
    # Repeated workflow
    ("workflow", "steps to", "how to", "pattern:"),
    # Tool/library usage
    ("import ", "pip install", "using ", "api call"),
    # Configuration
    ("config", "setup", "env var", "environment variable"),
    # Error that was debugged
    ("error:", "bug:", "root cause:", "workaround:"),
]

# Minimum session note length to consider
MIN_SESSION_LENGTH = 200

# Maximum lessons per run
MAX_LESSONS_PER_RUN = 5


def extract_session_text(session_path: Path) -> str:
    """Read a session note and return its text content."""
    try:
        return session_path.read_text(encoding="utf-8")
    except Exception:
        return ""


def has_reusable_pattern(text: str) -> bool:
    """Check if session text contains reusable patterns."""
    text_lower = text.lower()
    for keywords in PATTERN_SIGNALS:
        matches = sum(1 for kw in keywords if kw in text_lower)
        if matches >= 2:
            return True
    return False


def extract_lesson_title(text: str) -> str:
    """Extract a short title from session text."""
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()[:80]
    # Use first meaningful line
    for line in text.split("\n"):
        line = line.strip()
        if len(line) > 15 and not line.startswith("```"):
            return line[:80]
    return "Session insight"


def find_duplicate_lessons(conn: AnyConnection, title: str) -> bool:
    """Check if a similar lesson already exists."""
    title_lower = title.lower().strip()
    rows = conn.execute(
        "SELECT id, content FROM memories "
        "WHERE category = 'lessons' AND deleted_at IS NULL "
        "ORDER BY created_at DESC LIMIT 100"
    ).fetchall()
    for mid, content in rows:
        if content and title_lower in content.lower()[:200]:
            return True
        # Check for title similarity (first line of content)
        if content:
            first_line = content.split("\n")[0].strip().lstrip("#").strip().lower()
            if (
                title_lower
                and first_line
                and (title_lower in first_line or first_line in title_lower)
            ):
                return True
    return False


def scan_sessions_and_learn(
    conn: AnyConnection,
    days: int = 7,
    dry_run: bool = False,
) -> dict:
    """Scan recent session notes and extract reusable patterns.

    Returns: {"sessions_scanned": N, "lessons_created": N,
              "skipped_duplicates": N}
    """
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_ts = cutoff.timestamp()

    # Find recent session notes
    session_dir = resolve_active_memory_dir() / "sessions"
    if not session_dir.exists():
        return {"sessions_scanned": 0, "lessons_created": 0, "skipped_duplicates": 0}

    sessions_scanned = 0
    lessons_created = 0
    skipped_duplicates = 0

    for session_file in sorted(session_dir.glob("*.md"), reverse=True):
        # Check file age
        try:
            mtime = session_file.stat().st_mtime
            if mtime < cutoff_ts:
                continue
        except Exception:
            continue

        text = extract_session_text(session_file)
        if len(text) < MIN_SESSION_LENGTH:
            continue

        sessions_scanned += 1

        if not has_reusable_pattern(text):
            continue

        title = extract_lesson_title(text)
        if find_duplicate_lessons(conn, title):
            skipped_duplicates += 1
            continue

        if lessons_created >= MAX_LESSONS_PER_RUN:
            continue

        # Create lesson note
        lesson_content = f"# Lesson: {title}\n\n"
        lesson_content += f"Extracted from session: {session_file.name}\n"
        lesson_content += f"Extracted at: {datetime.now(timezone.utc).isoformat()}\n\n"
        lesson_content += text[:2000]  # Cap at 2000 chars

        # Determine tags from content
        tags = []
        text_lower = text.lower()
        if any(kw in text_lower for kw in ["error:", "bug:", "fix"]):
            tags.append("bugfix")
        if any(kw in text_lower for kw in ["import ", "pip ", "api"]):
            tags.append("tooling")
        if any(kw in text_lower for kw in ["config", "setup", "env"]):
            tags.append("configuration")
        if any(kw in text_lower for kw in ["workflow", "steps"]):
            tags.append("workflow")

        if not dry_run:
            from uuid import uuid4

            slug = f"cross-session-{uuid4().hex[:8]}"
            datetime.now(timezone.utc).isoformat()
            note_id = f"lessons/{slug}"
            # P0-5 fix: route the row write through save_pipeline.upsert_row
            # instead of a raw INSERT.  This keeps fitness_score, importance,
            # repo_id, valid_from, and the file_mtimes row consistent with
            # the canonical save_memory path.
            from save_pipeline import upsert_row

            upsert_row(
                conn,
                note_id,
                lesson_content,
                source_file=f"lessons/{slug}.md",
                tags=tags,
                category="lessons",
                pinned=False,
                tier="warm",
            )
            # Run save-pipeline indexing for the new lesson
            try:
                from save.indexers import (
                    _index_chunks,
                    _index_embedding,
                    _index_kg,
                    _index_facts,
                    _index_adaptive_retention,
                )
                from save.backlinks import (
                    _auto_semantic_backlinks,
                    _auto_fts_backlinks,
                )

                tags_list = tags if tags else []
                _index_chunks(conn, note_id, lesson_content)
                _index_embedding(
                    conn,
                    note_id,
                    lesson_content,
                    "lessons",
                    tags_list,
                    f"lessons/{slug}.md",
                )
                _index_kg(conn, note_id, lesson_content)
                _index_facts(conn, note_id, lesson_content)
                _auto_semantic_backlinks(conn, note_id, lesson_content)
                _auto_fts_backlinks(conn, note_id, lesson_content)
                _index_adaptive_retention(conn, note_id)
            except Exception as _ie:
                logger.debug("Indexing skipped for %s: %s", note_id, _ie)

        lessons_created += 1

    if not dry_run:
        conn.commit()

    return {
        "sessions_scanned": sessions_scanned,
        "lessons_created": lessons_created,
        "skipped_duplicates": skipped_duplicates,
        "dry_run": dry_run,
    }


def main():
    dry_run = "--dry-run" in sys.argv
    days = 7
    for arg in sys.argv[1:]:
        if arg.startswith("--days="):
            days = int(arg.split("=", 1)[1])

    env = os.environ.get("MEMORY_DB_PATH")
    db_path = (
        Path(env) if env is not None else resolve_active_memory_dir() / "memory.db"
    )
    if not db_path.exists():
        print(f"ERROR: no memory.db at {db_path}")
        sys.exit(1)

    from infra.db_write_queue import sqlite_write_queue

    conn = sqlite_write_queue.start_session(db_path)
    conn.execute("PRAGMA busy_timeout = 30000;")
    try:
        stats = scan_sessions_and_learn(conn, days=days, dry_run=dry_run)
        print(
            f"Cross-session learning: {stats['sessions_scanned']} sessions scanned, "
            f"{stats['lessons_created']} lessons created, "
            f"{stats['skipped_duplicates']} duplicates skipped"
            f"{' (dry run)' if dry_run else ''}"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
