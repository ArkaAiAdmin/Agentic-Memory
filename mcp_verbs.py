"""12-verb agent surface for agentic memory.

Each verb is a thin @mcp.tool() wrapper around existing functionality
with sensible defaults so the agent can call it with 1-2 params.

The underlying 81 ADMIN tools are still accessible via
memory_maintenance(operation="...") for power users.

Verbs:
  memory_search, memory_save, memory_recall, memory_note,
  memory_learn, memory_health, memory_audit, memory_organize,
  memory_share, memory_graph, memory_profile, memory_advanced
"""
from __future__ import annotations

import logging
from pathlib import Path

from mcp_common import (
    GLOBAL_MEM_DIR,
    _err,
    ErrorCode,
    get_memory_paths,
    logger,
    with_audit,
)
from mcp_instance import mcp

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_db_path(is_global: bool = False, db_path: str | None = None):
    """Resolve the active memory DB path."""
    if db_path:
        return Path(db_path)
    if is_global:
        return GLOBAL_MEM_DIR / "memory.db"
    _, local_mem, _ = get_memory_paths()
    return local_mem / "memory.db"


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------


@mcp.tool()
@with_audit("memory_search")
def memory_search(
    query: str,
    category: str = "",
    limit: int = 10,
    include_global: bool = True,
    mode: str = "hybrid",
) -> str:
    """Search memories by semantic + FTS5 hybrid search.

    The primary recall tool. Returns ranked memories matching the query.

    Args:
        query: Natural-language search query (required).
        category: Filter to a category (e.g. "lessons", "decisions").
        limit: Max results (default 10).
        include_global: Include global memories (default True).
        mode: "hybrid" (default), "semantic", "fts", "facts", "graph".
    """
    try:
        from search.orchestrator import search_memories

        db_path = _resolve_db_path()
        result = search_memories(
            db_path=db_path,
            query=query,
            category=category or None,
            limit=limit,
            include_global=include_global,
        )
        return result.get("results_blob", str(result))
    except Exception as e:
        logger.exception("in memory_search verb")
        return _err(ErrorCode.DB_ERROR, f"memory_search: {e}")


@mcp.tool()
@with_audit("memory_save")
def memory_save(
    content: str,
    category: str = "lessons",
    title_slug: str = "",
    tags: list[str] | None = None,
    pinned: bool = False,
    importance: int = 3,
    is_global: bool = False,
) -> str:
    """Save a memory note with sensible defaults.

    Args:
        content: The memory content (markdown).
        category: lessons / decisions / projects / preferences / sessions (default: lessons).
        title_slug: URL-friendly slug (auto-generated if empty).
        tags: Optional keyword tags.
        pinned: Pin to hot tier (default False).
        importance: 1-5 (default 3).
        is_global: Save to global memory (default False).
    """
    try:
        from save_pipeline import save_memory

        result = save_memory(
            content=content,
            category=category,
            title_slug=title_slug,
            tags=tags or [],
            pinned=pinned,
            importance=importance,
            is_global=is_global,
        )
        return str(result)
    except Exception as e:
        logger.exception("in memory_save verb")
        return _err(ErrorCode.DB_ERROR, f"memory_save: {e}")


@mcp.tool()
@with_audit("memory_recall")
def memory_recall(query: str = "", session_id: str = "") -> str:
    """Recall context for the current session or a named thread.

    Combines session_start + recall_context into one call.
    If no query is given, returns recent session activity.

    Args:
        query: What to recall (default: recent activity).
        session_id: Specific session/thread to recall.
    """
    try:
        from search.orchestrator import search_memories

        db_path = _resolve_db_path()
        q = query or "recent session activity"
        result = search_memories(
            db_path=db_path,
            query=q,
            limit=5,
            include_global=True,
        )
        return result.get("results_blob", str(result))
    except Exception as e:
        logger.exception("in memory_recall verb")
        return _err(ErrorCode.DB_ERROR, f"memory_recall: {e}")


@mcp.tool()
@with_audit("memory_note")
def memory_note(
    note_id: str,
    action: str = "read",
    content: str = "",
    category: str = "",
    title_slug: str = "",
    tags: list[str] | None = None,
) -> str:
    """CRUD operations on a specific memory note.

    Args:
        note_id: The note ID (e.g. "lessons/my-note").
        action: "read" | "update" | "delete" | "restore" | "supersede".
        content: New content (required for update).
        category: New category (for update).
        title_slug: New slug (for update).
        tags: New tags (for update).
    """
    try:
        if action == "read":
            from memory_delete import get_memory

            result = get_memory(note_id)
            return str(result)
        elif action == "delete":
            from memory_delete import delete_memory

            result = delete_memory(note_id)
            return str(result)
        elif action == "restore":
            from memory_delete import restore_memory

            result = restore_memory(note_id)
            return str(result)
        elif action == "update":
            from save_pipeline import save_memory

            result = save_memory(
                content=content,
                category=category or "lessons",
                title_slug=title_slug or note_id.split("/")[-1],
                tags=tags or [],
                db_path=None,
            )
            return str(result)
        elif action == "supersede":
            from save_pipeline import memory_supersede_db
            from pathlib import Path

            db_path = _resolve_db_path()
            new_note_id = title_slug or note_id
            ok, err = memory_supersede_db(
                db_path=db_path,
                old_id=note_id,
                new_id=new_note_id,
            )
            return str(ok) if ok else str(err)
        else:
            return _err(
                ErrorCode.INVALID_PARAMS,
                f"Unknown action '{action}'. Use: read, update, delete, restore, supersede",
            )
    except Exception as e:
        logger.exception("in memory_note verb")
        return _err(ErrorCode.DB_ERROR, f"memory_note: {e}")


@mcp.tool()
@with_audit("memory_learn")
def memory_learn(
    content: str,
    as_skill: bool = False,
    skill_name: str = "",
    category: str = "lessons",
    tags: list[str] | None = None,
) -> str:
    """Save a lesson or compile a skill from content.

    Auto-categorizes and tags the memory. Optionally compiles a skill.

    Args:
        content: The lesson/skill content.
        as_skill: If True, compile as a skill (default False).
        skill_name: Skill directory name (required if as_skill=True).
        category: Target category (default: lessons).
        tags: Additional tags.
    """
    try:
        from save_pipeline import save_memory
        from mcp_maintenance import memory_compile_skill

        # Save the memory
        result = save_memory(
            content=content,
            category=category,
            title_slug=skill_name or "",
            tags=tags or ["learned"],
            importance=4,
        )
        if as_skill and skill_name:
            skill_result = memory_compile_skill(
                lesson_slug=skill_name,
                skill_name=skill_name,
                primary_triggers=["learned"],
            )
            return f"Saved + compiled skill: {skill_name}\n{skill_result}"
        return str(result)
    except Exception as e:
        logger.exception("in memory_learn verb")
        return _err(ErrorCode.DB_ERROR, f"memory_learn: {e}")


@mcp.tool()
@with_audit("memory_audit")
def memory_audit(
    hours: int = 24,
    limit: int = 20,
    include_errors: bool = True,
) -> str:
    """Review recent memory activity, errors, and system health.

    Combines audit_query + circuit_breaker_status into one call.

    Args:
        hours: Look back window (default 24h).
        limit: Max results (default 20).
        include_errors: Include error entries (default True).
    """
    try:
        from mcp_maintenance import memory_audit_query, memory_circuit_breaker_status

        since_ts = None
        if hours > 0:
            import time

            since_ts = time.time() - (hours * 3600)

        audit = memory_audit_query(
            since_ts=since_ts,
            only_errors=not include_errors,
            limit=limit,
        )
        cb = memory_circuit_breaker_status(limit=5)
        return f"## Recent Activity\n{audit}\n\n## Circuit Breaker\n{cb}"
    except Exception as e:
        logger.exception("in memory_audit verb")
        return _err(ErrorCode.DB_ERROR, f"memory_audit: {e}")


@mcp.tool()
@with_audit("memory_organize")
def memory_organize(
    target: str = "safe_default",
    dry_run: bool = False,
) -> str:
    """Run safe memory maintenance batch.

    Targets:
      safe_default: compact + consolidate + rewrite_links
      full: safe_default + backfill + dedup + purge_expired
      compact: FTS5 compact only
      dedup: KG entity dedup only

    Args:
        target: Which batch to run (default: safe_default).
        dry_run: Preview without changes (default False).
    """
    try:
        from mcp_maintenance import (
            memory_compact,
            memory_consolidate,
            memory_rewrite_links,
            memory_backfill_all,
            memory_purge_expired,
            memory_duplicates,
        )

        if target == "safe_default":
            results = [
                ("compact", memory_compact(dry_run=dry_run)),
                ("consolidate", memory_consolidate()),
                ("rewrite_links", memory_rewrite_links()),
            ]
        elif target == "full":
            results = [
                ("compact", memory_compact(dry_run=dry_run)),
                ("consolidate", memory_consolidate()),
                ("rewrite_links", memory_rewrite_links()),
                ("backfill", memory_backfill_all(backfill_mode="health")),
                ("purge_expired", memory_purge_expired()),
                ("dedup", memory_duplicates(threshold=0.85)),
            ]
        elif target == "compact":
            return memory_compact(dry_run=dry_run)
        elif target == "dedup":
            return memory_duplicates(threshold=0.85)
        else:
            return _err(
                ErrorCode.INVALID_PARAMS,
                f"Unknown target '{target}'. Use: safe_default, full, compact, dedup",
            )

        lines = [f"# memory_organize({target}){' [DRY RUN]' if dry_run else ''}", ""]
        for name, result in results:
            lines.append(f"## {name}")
            lines.append(str(result))
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        logger.exception("in memory_organize verb")
        return _err(ErrorCode.DB_ERROR, f"memory_organize: {e}")


@mcp.tool()
@with_audit("memory_share")
def memory_share(
    note_id: str,
    share_with: str = "",
    action: str = "list",
) -> str:
    """Share memories with other agents or view shared pool.

    Args:
        note_id: Memory to share (required for action=share).
        share_with: Target agent ID (for action=share).
        action: "list" | "share" | "import" | "stats".
    """
    try:
        from mcp_maintenance import (
            memory_shared_list,
            memory_share,
            memory_shared_import,
            memory_shared_stats,
        )

        if action == "list":
            return memory_shared_list()
        elif action == "share":
            if not share_with:
                return _err(ErrorCode.INVALID_PARAMS, "share_with required for action=share")
            return memory_share(note_id=note_id, share_agent_id=share_with)
        elif action == "import":
            return memory_shared_import(shared_id=note_id, target_agent_id=share_with)
        elif action == "stats":
            return memory_shared_stats()
        else:
            return _err(ErrorCode.INVALID_PARAMS, f"Unknown action '{action}'")
    except Exception as e:
        logger.exception("in memory_share verb")
        return _err(ErrorCode.DB_ERROR, f"memory_share: {e}")


@mcp.tool()
@with_audit("memory_graph")
def memory_graph(
    query: str = "",
    start: str = "",
    edge_patterns: str = "",
    max_depth: int = 2,
    action: str = "explore",
) -> str:
    """Explore the knowledge graph.

    Args:
        query: Natural language KG query (for action=explore).
        start: Starting entity/node ID (for action=traverse).
        edge_patterns: Edge type filter (for action=traverse).
        max_depth: Max traversal depth (default 2).
        action: "explore" | "traverse" | "shortest_path" | "stats".
    """
    try:
        from mcp_maintenance import (
            memory_facts_list,
            memory_graph_stats,
            memory_graph_shortest_path,
            memory_graph_traverse,
        )

        if action == "explore":
            facts = memory_facts_list(facts_limit=20)
            stats = memory_graph_stats()
            return f"## KG Facts\n{facts}\n\n## Stats\n{stats}"
        elif action == "traverse":
            return memory_graph_traverse(start=start, edge_patterns=edge_patterns)
        elif action == "shortest_path":
            return memory_graph_shortest_path(source=start, target=edge_patterns, max_depth=max_depth)
        elif action == "stats":
            return memory_graph_stats()
        else:
            return _err(ErrorCode.INVALID_PARAMS, f"Unknown action '{action}'")
    except Exception as e:
        logger.exception("in memory_graph verb")
        return _err(ErrorCode.DB_ERROR, f"memory_graph: {e}")


@mcp.tool()
@with_audit("memory_profile")
def memory_profile(
    action: str = "stats",
    agent_id: str = "",
) -> str:
    """View user profile, agent scopes, ARC stats, and cached skills.

    Args:
        action: "stats" | "user" | "agents" | "skills" | "arc".
        agent_id: Agent ID (for action=agents).
    """
    try:
        from mcp_maintenance import (
            memory_profile_stats,
            memory_user_profile,
            memory_agent_list,
            memory_agent_init,
            memory_arc_stats,
            memory_list_skills,
        )

        if action == "stats":
            return memory_profile_stats()
        elif action == "user":
            return memory_user_profile()
        elif action == "agents":
            if agent_id:
                return memory_agent_init(agent_id=agent_id)
            return memory_agent_list()
        elif action == "skills":
            return memory_list_skills(limit=50)
        elif action == "arc":
            return memory_arc_stats()
        else:
            return _err(ErrorCode.INVALID_PARAMS, f"Unknown action '{action}'")
    except Exception as e:
        logger.exception("in memory_profile verb")
        return _err(ErrorCode.DB_ERROR, f"memory_profile: {e}")


@mcp.tool()
@with_audit("memory_advanced")
def memory_advanced(operation: str, **kwargs: str) -> str:
    """Power user escape hatch — pass through to any memory_maintenance operation.

    Use this when a verb doesn't cover your use case.

    Args:
        operation: Any memory_maintenance operation name.
        **kwargs: Operation-specific parameters.
    """
    try:
        from mcp_maintenance import memory_maintenance

        return memory_maintenance(operation=operation, **kwargs)
    except Exception as e:
        logger.exception("in memory_advanced verb")
        return _err(ErrorCode.DB_ERROR, f"memory_advanced: {e}")
