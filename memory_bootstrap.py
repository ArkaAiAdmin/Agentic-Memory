#!/usr/bin/env python3
"""
Memory Bootstrap — run at session start to provide context.

Outputs a compact summary of:
1. Pinned notes (always relevant)
2. High-importance notes (importance > 0.7)
3. Recent notes (last 7 days, top 10 by score)
4. Active reminders

If a compaction event occurred recently (within the last hour),
the full pre-compaction recovery note is prepended as the first
section so the agent knows context was lost and what to recover.
"""

import os
import sys
import json
import time
import argparse
from pathlib import Path

from infra.memory_config import get_memory_paths


def _get_sessions_dir() -> Path:

    _, local_mem, _ = get_memory_paths()
    return local_mem / "sessions"


def _get_recent_compaction() -> str | None:
    """Return the full text of the most recent compaction note.

    No age limit — the most recent compaction is always worth surfacing
    after a context reset. Previously capped at COMPACTION_WINDOW_SEC (1h)
    which silently dropped valid recovery content from older compactions.
    """
    sessions_dir = _get_sessions_dir()
    try:
        state_file = sessions_dir / ".context_monitor_state.json"
        if state_file.exists():
            with open(state_file) as f:
                state = json.load(f)
            last_compaction = state.get("last_compaction_time", 0)
            if last_compaction:
                ts = time.strftime("%Y-%m-%d_%H-%M-%S", time.gmtime(last_compaction))
                path = sessions_dir / f"compaction-save-{ts}.md"
                if path.exists():
                    return path.read_text()[:3000]
    except Exception:
        pass

    # Fallback: scan for the most recently-modified compaction-save-*.md
    try:
        candidates = sorted(
            sessions_dir.glob("compaction-save-*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0].read_text()[:3000]
    except Exception:
        pass

    return None


def _get_recent_sessions(conn, project_root: str = "", limit: int = 3) -> list[dict]:
    """Return the most recent sessions for a project (read-only, direct SQL)."""
    try:
        query = (
            "SELECT id, started_at, ended_at, status, parent_session_id FROM sessions "
        )
        params: tuple = ()
        if project_root:
            query += "WHERE project_root = ? "
            params = (project_root,)
        query += "ORDER BY started_at DESC LIMIT ?"
        params = params + (limit,)
        rows = conn.execute(query, params).fetchall()
        return [
            {
                "id": r[0],
                "started_at": r[1],
                "ended_at": r[2],
                "status": r[3],
                "parent_session_id": r[4],
            }
            for r in rows
        ]
    except Exception:
        return []


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 2026-06-22 (C4 fix): these setdefault calls are redundant. The config
# singleton in `config.py` already defaults these features to True (see
# `features.knowledge_graph` and `features.self_directed`). Setting them
# in the bootstrap process environment was hiding the operator's ability
# to opt out via TOML — an operator who wrote `self_directed = false` in
# `memory.toml` would have their setting silently overridden because
# `setdefault` runs before `get_config()` resolves the TOML.
#
# We no longer pre-populate these env vars here. If a future caller
# needs to force a feature on for the bootstrap pass, they can set the
# env var explicitly before importing this module.

from infra.memory_common import safe_close_db


def get_preferences(conn):
    from agent_context import get_agent

    ctx = get_agent()
    namespace = ctx.namespace
    if namespace != "default":
        rows = conn.execute(
            "SELECT id, content, category, importance_score, tags FROM memories "
            "WHERE category = 'preferences' AND deleted_at IS NULL AND (id LIKE ? OR id NOT LIKE 'agents/%') "
            "ORDER BY importance_score DESC, updated_at DESC LIMIT 5",
            (f"agents/{namespace}/%",),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, content, category, importance_score, tags FROM memories "
            "WHERE category = 'preferences' AND deleted_at IS NULL AND id NOT LIKE 'agents/%' "
            "ORDER BY importance_score DESC, updated_at DESC LIMIT 5"
        ).fetchall()
    return [
        {
            "id": r[0],
            "content": r[1],
            "category": r[2],
            "importance": r[3],
            "tags": r[4],
        }
        for r in rows
    ]


def get_pinned_notes(conn):
    from agent_context import get_agent

    ctx = get_agent()
    namespace = ctx.namespace
    if namespace != "default":
        rows = conn.execute(
            "SELECT id, content, category, importance_score, tags FROM memories "
            "WHERE pinned = 1 AND deleted_at IS NULL AND (id LIKE ? OR id NOT LIKE 'agents/%') "
            "ORDER BY importance_score DESC LIMIT 10",
            (f"agents/{namespace}/%",),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, content, category, importance_score, tags FROM memories "
            "WHERE pinned = 1 AND deleted_at IS NULL AND id NOT LIKE 'agents/%' "
            "ORDER BY importance_score DESC LIMIT 10"
        ).fetchall()
    return [
        {
            "id": r[0],
            "content": r[1][:300],
            "category": r[2],
            "importance": r[3],
            "tags": r[4],
        }
        for r in rows
    ]


def get_high_importance(conn):
    from agent_context import get_agent

    ctx = get_agent()
    namespace = ctx.namespace
    if namespace != "default":
        rows = conn.execute(
            "SELECT id, content, category, importance_score, tags FROM memories "
            "WHERE importance_score > 0.7 AND deleted_at IS NULL AND pinned = 0 AND (id LIKE ? OR id NOT LIKE 'agents/%') "
            "ORDER BY importance_score DESC LIMIT 10",
            (f"agents/{namespace}/%",),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, content, category, importance_score, tags FROM memories "
            "WHERE importance_score > 0.7 AND deleted_at IS NULL AND pinned = 0 AND id NOT LIKE 'agents/%' "
            "ORDER BY importance_score DESC LIMIT 10"
        ).fetchall()
    return [
        {
            "id": r[0],
            "content": r[1][:200],
            "category": r[2],
            "importance": r[3],
            "tags": r[4],
        }
        for r in rows
    ]


def get_recent_notes(conn, days=7):
    cutoff = time.time() - days * 86400
    from agent_context import get_agent

    ctx = get_agent()
    namespace = ctx.namespace
    if namespace != "default":
        rows = conn.execute(
            "SELECT id, content, category, importance_score, tags, updated_at "
            "FROM memories WHERE deleted_at IS NULL "
            "AND updated_at > ? AND (id LIKE ? OR id NOT LIKE 'agents/%') "
            "ORDER BY updated_at DESC LIMIT 10",
            (str(cutoff), f"agents/{namespace}/%"),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, content, category, importance_score, tags, updated_at "
            "FROM memories WHERE deleted_at IS NULL "
            "AND updated_at > ? AND id NOT LIKE 'agents/%' "
            "ORDER BY updated_at DESC LIMIT 10",
            (str(cutoff),),
        ).fetchall()
    return [
        {
            "id": r[0],
            "content": r[1][:200],
            "category": r[2],
            "importance": r[3],
            "tags": r[4],
        }
        for r in rows
    ]


def get_stats(conn):
    from agent_context import get_agent

    ctx = get_agent()
    namespace = ctx.namespace
    if namespace != "default":
        total = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL AND (id LIKE ? OR id NOT LIKE 'agents/%')",
            (f"agents/{namespace}/%",),
        ).fetchone()[0]
        pinned = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE pinned = 1 AND deleted_at IS NULL AND (id LIKE ? OR id NOT LIKE 'agents/%')",
            (f"agents/{namespace}/%",),
        ).fetchone()[0]

        cursor = conn.execute("PRAGMA table_info(kg_facts)")
        cols = [row[1] for row in cursor.fetchall()]
        if "subject_entity_id" in cols and "object_entity_id" in cols:
            entities = conn.execute(
                "SELECT COUNT(DISTINCT e.id) FROM kg_entities e "
                "JOIN kg_facts f ON (e.id = f.subject_entity_id OR e.id = f.object_entity_id) "
                "WHERE f.source_memory LIKE ? OR f.source_memory NOT LIKE 'agents/%' OR f.source_memory IS NULL",
                (f"agents/{namespace}/%",),
            ).fetchone()[0]
        else:
            entities = conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0]

        facts = conn.execute(
            "SELECT COUNT(*) FROM kg_facts WHERE source_memory LIKE ? OR source_memory NOT LIKE 'agents/%' OR source_memory IS NULL",
            (f"agents/{namespace}/%",),
        ).fetchone()[0]
    else:
        total = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL AND id NOT LIKE 'agents/%'"
        ).fetchone()[0]
        pinned = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE pinned = 1 AND deleted_at IS NULL AND id NOT LIKE 'agents/%'"
        ).fetchone()[0]

        cursor = conn.execute("PRAGMA table_info(kg_facts)")
        cols = [row[1] for row in cursor.fetchall()]
        if "subject_entity_id" in cols and "object_entity_id" in cols:
            entities = conn.execute(
                "SELECT COUNT(DISTINCT e.id) FROM kg_entities e "
                "JOIN kg_facts f ON (e.id = f.subject_entity_id OR e.id = f.object_entity_id) "
                "WHERE f.source_memory NOT LIKE 'agents/%' OR f.source_memory IS NULL"
            ).fetchone()[0]
        else:
            entities = conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0]

        facts = conn.execute(
            "SELECT COUNT(*) FROM kg_facts WHERE source_memory NOT LIKE 'agents/%' OR source_memory IS NULL"
        ).fetchone()[0]
    return {
        "total_notes": total,
        "pinned": pinned,
        "kg_entities": entities,
        "kg_facts": facts,
    }


def format_summary(
    pinned,
    high_importance,
    recent,
    stats,
    preferences=None,
    sessions=None,
    session_notes=None,
):
    lines = []
    lines.append(
        f"Memory System: {stats['total_notes']} notes, {stats['pinned']} pinned, "
        f"{stats['kg_entities']} KG entities, {stats['kg_facts']} facts"
    )

    if sessions:
        lines.append("\n## Recent Sessions")
        for s in sessions:
            pid = s.get("parent_session_id", "")
            parent_ref = f" (parent: {pid[:12]})" if pid else ""
            ended = s.get("ended_at") or "active"
            lines.append(
                f"- **{s['id'][:12]}** [{s['status']}] started {s['started_at'][:16]}"
                f" — ended: {ended[:16]}{parent_ref}"
            )

    if session_notes:
        lines.append("\n## Pinned Session Notes")
        for n in session_notes:
            tags = f" [{n['tags']}]" if n["tags"] else ""
            lines.append(
                f"- **{n['id']}** ({n['category'] or 'uncategorized'}{tags}): {n['content']}"
            )

    if preferences:
        lines.append("\n## ⚑ Preferences")
        for n in preferences:
            tags = f" [{n['tags']}]" if n["tags"] else ""
            lines.append(
                f"- **{n['id']}** ({n['category'] or 'uncategorized'}{tags}): {n['content']}"
            )

    if pinned:
        lines.append("\n## Pinned Notes")
        for n in pinned:
            tags = f" [{n['tags']}]" if n["tags"] else ""
            lines.append(
                f"- **{n['id']}** ({n['category'] or 'uncategorized'}{tags}): {n['content']}"
            )

    if high_importance:
        lines.append("\n## High Importance")
        for n in high_importance:
            lines.append(f"- **{n['id']}** (imp={n['importance']:.2f}): {n['content']}")

    if recent:
        lines.append("\n## Recent (7 days)")
        for n in recent:
            lines.append(f"- **{n['id']}**: {n['content']}")

    if (
        not preferences
        and not pinned
        and not high_importance
        and not recent
        and not session_notes
    ):
        lines.append(
            "\nNo preferences, pinned, high-importance, or recent notes found."
        )

    return "\n".join(lines)


def get_bootstrap_summary(db_path: str | None = None) -> str:
    if db_path is not None:
        resolved = Path(db_path)
    elif os.environ.get("MEMORY_DB_PATH"):
        resolved = Path(os.environ["MEMORY_DB_PATH"])
    else:
        from infra.infrastructure import resolve_active_memory_dir

        resolved = resolve_active_memory_dir() / "memory.db"
    if not resolved.exists():
        return "No memory.db found."

    from infra.db import open_db
    with open_db(resolved, timeout=30.0, pooled=True, write=False) as conn:
        project_root = None
        try:
            project_root = Path.cwd()
        except Exception:
            pass
        pinned = get_pinned_notes(conn)
        high_importance = get_high_importance(conn)
        recent = get_recent_notes(conn)
        stats = get_stats(conn)
        preferences = get_preferences(conn)
        sessions = _get_recent_sessions(
            conn, project_root=str(project_root) if project_root else "", limit=3
        )
        session_notes = []
        try:
            session_notes = [
                {
                    "id": r[0],
                    "content": r[1][:300],
                    "category": r[2],
                    "importance": r[3],
                    "tags": r[4],
                }
                for r in conn.execute(
                    "SELECT id, content, category, importance_score, tags "
                    "FROM memories WHERE category='sessions' AND pinned=1 "
                    "AND deleted_at IS NULL "
                    "ORDER BY created_at DESC LIMIT 5"
                ).fetchall()
            ]
        except Exception:
            pass
        summary = format_summary(
            pinned,
            high_importance,
            recent,
            stats,
            preferences,
            sessions=sessions,
            session_notes=session_notes,
        )

        compaction = _get_recent_compaction()

        if compaction:
            header = (
                "\n\n"
                "========================================\n"
                "  COMPACTION RECOVERY\n"
                "  Context was lost in a recent compaction.\n"
                "  Read the section below FIRST before doing anything else.\n"
                "========================================\n\n"
            )
            summary = header + compaction + "\n\n" + summary

        return summary


def main(db_path: str | None = None):
    parser = argparse.ArgumentParser(description="Memory bootstrap for session start")
    parser.add_argument(
        "--full", action="store_true", help="Full content of pinned notes"
    )
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    if db_path is not None:
        resolved = Path(db_path)
    elif os.environ.get("MEMORY_DB_PATH"):
        resolved = Path(os.environ["MEMORY_DB_PATH"])
    else:
        from infra.infrastructure import resolve_active_memory_dir

        resolved = resolve_active_memory_dir() / "memory.db"
    if not resolved.exists():
        print("No memory.db found.")
        sys.exit(1)

    from infra.db import open_db
    with open_db(resolved, timeout=30.0, pooled=True, write=False) as conn:
        pinned = get_pinned_notes(conn)
        high_importance = get_high_importance(conn)
        recent = get_recent_notes(conn)
        stats = get_stats(conn)
        preferences = get_preferences(conn)

        if args.json:
            print(
                json.dumps(
                    {
                        "preferences": preferences,
                        "pinned": pinned,
                        "high_importance": high_importance,
                        "recent": recent,
                        "stats": stats,
                    },
                    indent=2,
                )
            )
        elif args.full:
            for n in list(preferences) + list(pinned) + list(high_importance):
                print(f"\n{'=' * 60}")
                print(f"{n['id']} ({n.get('category', '?')})")
                print(f"{'=' * 60}")
                print(n["content"])
        else:
            print(format_summary(pinned, high_importance, recent, stats, preferences))


if __name__ == "__main__":
    main()
