#!/usr/bin/env python3
"""
Session Reflection Script for Agentic Memory System

Run at the END of every session to ensure learnings are saved.
Checks what changed during the session and prompts for memory saves.

Usage:
    python3 session_reflect.py <project_memory_dir>
    python3 session_reflect.py <project_memory_dir>
"""

import sys
import os
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timedelta


def get_recent_changes(memory_dir: Path, hours: int = 4) -> dict:
    """Find files modified in the last N hours."""
    cutoff = datetime.now() - timedelta(hours=hours)
    changes = {"new": [], "modified": [], "deleted": []}

    # Check for recently modified .md files
    for md in memory_dir.rglob("*.md"):
        if "global" in md.parts:
            continue
        if md.name == "MEMORY.md":
            continue
        try:
            mtime = datetime.fromtimestamp(md.stat().st_mtime)
            if mtime > cutoff:
                rel = str(md.relative_to(memory_dir))
                # Check if it existed before
                db = sqlite3.connect(str(memory_dir / "memory.db"), timeout=5)
                md_id = rel.replace(".md", "")
                existing = db.execute(
                    "SELECT id FROM memories WHERE id = ?", (md_id,)
                ).fetchone()
                db.close()
                if existing:
                    changes["modified"].append(rel)
                else:
                    changes["new"].append(rel)
        except (OSError, PermissionError):
            pass

    return changes


def check_unsaved_context(memory_dir: Path) -> list:
    """Check for common patterns that should be saved as memories."""
    suggestions = []

    # Check if there are recent files not in MEMORY.md
    memory_md = memory_dir / "MEMORY.md"
    if memory_md.exists():
        content = memory_md.read_text()
        import re
        links = set(re.findall(r"\[\[([^\]]+)\]\]", content))
        links = {l.lstrip("/") for l in links}

        for md in memory_dir.rglob("*.md"):
            if "global" in md.parts or md.name == "MEMORY.md":
                continue
            rel = str(md.relative_to(memory_dir))
            if rel not in links and rel.replace(".md", "") not in links:
                suggestions.append(f"File not indexed: {rel}")

    # Check for test artifacts that should be cleaned
    db_path = memory_dir / "memory.db"
    if db_path.exists():
        db = sqlite3.connect(str(db_path), timeout=5)
        test_entries = db.execute(
            "SELECT id FROM memories WHERE id LIKE '%test%' OR id LIKE '%e2e%' OR id LIKE '%temp%'"
        ).fetchall()
        db.close()
        if test_entries:
            suggestions.append(f"Test/temp entries in DB: {[e[0] for e in test_entries]}")

    return suggestions


def print_reflection_prompt(project_dir: str):
    """Print the reflection prompt for the agent."""
    memory_dir = Path(project_dir) / "memory"
    if not memory_dir.exists():
        print(f"No memory directory found at {memory_dir}")
        return

    print("=" * 60)
    print("SESSION REFLECTION — MANDATORY BEFORE YIELDING")
    print("=" * 60)

    # Recent changes
    changes = get_recent_changes(memory_dir)
    if changes["new"]:
        print(f"\nNew files created this session ({len(changes['new'])}):")
        for f in changes["new"]:
            print(f"  + {f}")

    if changes["modified"]:
        print(f"\nFiles modified this session ({len(changes['modified'])}):")
        for f in changes["modified"]:
            print(f"  ~ {f}")

    # Unsaged context
    suggestions = check_unsaved_context(memory_dir)
    if suggestions:
        print(f"\nItems needing attention ({len(suggestions)}):")
        for s in suggestions:
            print(f"  ! {s}")

    # DB state
    db_path = memory_dir / "memory.db"
    if db_path.exists():
        db = sqlite3.connect(str(db_path), timeout=5)
        count = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        recent = db.execute(
            "SELECT COUNT(*) FROM memories WHERE created_at > datetime('now', '-4 hours')"
        ).fetchone()[0]
        db.close()
        print(f"\nDB state: {count} total memories, {recent} created this session")

    # Global DB
    global_db = Path.home() / ".config" / "agentic-memory" / "memory.db"
    if global_db.exists():
        db = sqlite3.connect(str(global_db), timeout=5)
        g_count = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        db.close()
        print(f"Global DB: {g_count} memories")

    # The mandatory prompt
    print("\n" + "=" * 60)
    print("BEFORE ENDING THIS SESSION, YOU MUST:")
    print("=" * 60)
    print("1. Did you learn anything new? → memory_save() it")
    print("2. Did you solve a bug? → Save the fix as a lesson")
    print("3. Did you make a decision? → Save as an ADR")
    print("4. Did you find a pitfall? → Save to global lessons")
    print("5. Run memory_audit() to check health")
    print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 session_reflect.py <project_memory_dir>")
        print("Example: python3 session_reflect.py /path/to/project/memory")
        sys.exit(1)

    project_dir = sys.argv[1]
    # If they pass the memory dir directly, go up one level
    if Path(project_dir).name == "memory":
        project_dir = str(Path(project_dir).parent)

    print_reflection_prompt(project_dir)
