"""
Search MCP tools — memory_search, memory_semantic_search, memory_recall_context, memory_session_start.
"""

import _bootstrap_path  # noqa: E402
import os
import sys
from pathlib import Path


from pathlib import Path
from typing import Any, Optional, cast

from mcp_common import (
    _resolve_memory_dir,
    _run_subprocess_output,
    _search_cache,
    GLOBAL_SCRIPTS_DIR,
    GLOBAL_MEM_DIR,
    get_memory_paths,
    logger,
    _err,
    ErrorCode,
    with_audit,
    resolve_active_memory_dir,
)
from mcp_instance import mcp
import search_pipeline
from search_pipeline import search_memories as _search_memories_impl
from search_pipeline import _bb2_resolve, _bb2_record_turn

# B1 fix (2026-06-22): alias the canonical implementation so
# ``from mcp_search import search_memories`` resolves to the same
# function object as ``from search_pipeline import search_memories``.
# Earlier this module re-implemented the wrapper in-place (with the
# same signature), which silently shadowed any monkey-patch applied to
# the canonical function. The two definitions were identical but the
# identity wasn't shared, breaking ``is`` checks in tests.
search_memories = _search_memories_impl

import json
import os
import sys
import subprocess
import time
from datetime import datetime

# Spaced repetition: best-effort, never blocks search
_SpacedRepetition: Any
try:
    from spaced_repetition import SpacedRepetition as _SpacedRepetition
except ImportError:
    _SpacedRepetition = None


def _record_spaced_repetition(db_path: Path, result_items: list, query: str) -> None:
    if _SpacedRepetition is None:
        return
    try:
        sr = _SpacedRepetition(db_path)
        try:
            if result_items:
                for item in result_items:
                    note_id = item.get("id") if isinstance(item, dict) else None
                    if note_id:
                        sr.record_success(str(note_id))
            else:
                sr.record_failure(query)
        finally:
            sr.close()
    except Exception:
        logger.warning("Spaced repetition recording failed for query %r", query)
        pass


# B1 fix (2026-06-22): remove the in-place reimplementation. ``search_memories``
# is now an alias to the canonical implementation (see top of file), so
# ``from mcp_search import search_memories`` and
# ``from search_pipeline import search_memories`` resolve to the same
# function object. Earlier this was a fresh function with the same
# signature, so identity checks (``is``) in tests silently failed and
# monkey-patches applied to the canonical function were bypassed.


@mcp.tool()
@with_audit("memory_search")
def memory_search(
    query: str,
    limit: int = 5,
    rerank: bool = True,
    boost_pinned: bool = True,
    recency_weight: float = 0.1,
    include_global: bool = True,
    include_invalid: bool = True,
    deep_rerank: bool = False,
    include_facts: bool = True,
    fact_limit: int = 5,
) -> str:
    """Perform FTS5 (full-text) and semantic hybrid search across local and global memories.

    USE THIS TOOL WHEN:
    - You start a new session or need context about the user's workspace, historical code guidelines, past decisions, or preferences.
    - You want to retrieve relevant memories before modifying/adding code, resolving an issue, or saving new memories. Always search first to prevent duplicate memories!

    ARGUMENTS:
    - query: The search query string. Keep it descriptive (e.g. 'NextJS authentication configuration').
    - limit: Max number of memory results to return (default 5).
    - rerank: If True, uses the local cross-encoder model to rerank candidate documents for higher precision. Default is True.
    - boost_pinned: If True, elevates pinned ('hot') memories to the top. Default is True.
    - recency_weight: Weight given to note recency. Default is 0.1.
    - include_global: If True, includes memories from the global (~/.config/agentic-memory/memory/) path. Default is True.
    - include_invalid: If False, excludes memories that are expired or superseded. Default is True.
    - deep_rerank: If True, runs a deep transformer reranker (highest accuracy, but adds 1-3 seconds latency). Default is False.
    - include_facts: If True, also queries and appends matching knowledge graph facts. Default is True.
    - fact_limit: Max number of related facts to return. Default is 5.

    RETURNS:
    A human-readable formatted string listing the ranked memories, their content, category, tags, and related facts.
    """
    resolution_note = ""
    expanded_query = query
    try:
        res = _bb2_resolve(query)
        if res["reused"]:
            expanded_query = res["expanded_query"]
            resolution_note = f"[BB2: resolved '{query}' using prior turn terms: {', '.join(res['added_terms'])}]\n"
    except Exception as exc:
        logger.debug("BB2 resolve failed for %r: %s", query, exc)
        expanded_query = query
    active_dir = _resolve_memory_dir()
    if os.environ.get("MEMORY_DB_PATH"):
        local_mem = active_dir
        global_mem = Path(GLOBAL_MEM_DIR)
    else:
        _, local_mem, global_mem = get_memory_paths()
    local_db = local_mem / "memory.db"
    global_db = global_mem / "memory.db"

    local_results: dict[str, Any] = {"results": [], "count": 0, "output": ""}
    if local_db.exists():
        try:
            local_results = search_memories(
                local_db,
                expanded_query,
                limit,
                include_global=False,
                rerank=rerank,
                boost_pinned=boost_pinned,
                recency_weight=recency_weight,
                include_invalid=include_invalid,
                deep_rerank=deep_rerank,
                include_facts=include_facts,
                fact_limit=fact_limit,
            )
        except Exception as exc:
            logger.warning("Local search failed for query %r: %s", expanded_query, exc)

    if include_global and global_db.exists() and local_results["count"] < 3:
        try:
            global_results: dict[str, Any] = search_memories(
                global_db,
                expanded_query,
                limit,
                include_global=False,
                rerank=rerank,
                boost_pinned=boost_pinned,
                recency_weight=recency_weight,
                include_invalid=include_invalid,
                deep_rerank=deep_rerank,
                include_facts=include_facts,
                fact_limit=fact_limit,
            )
        except Exception as exc:
            logger.warning("Global search failed for query %r: %s", expanded_query, exc)
            global_results = {"results": [], "count": 0, "output": ""}
        if global_results["count"] > 0:
            sep = (
                "\n\n---\nGLOBAL MEMORY RESULTS:\n"
                if local_results["output"]
                else "---\nGLOBAL MEMORY RESULTS:\n"
            )
            out = f"{local_results['output']}{sep}{global_results['output']}"
            try:
                _bb2_record_turn(
                    query,
                    local_results.get("raw_results", [])
                    + global_results.get("raw_results", []),
                )
            except Exception as exc:
                logger.debug("BB2 record_turn failed: %s", exc)
            _record_spaced_repetition(
                local_db if local_db.exists() else global_db,
                local_results.get("results", []) + global_results.get("results", []),
                query,
            )
            return (resolution_note + out) if resolution_note else out
        try:
            _bb2_record_turn(query, local_results.get("raw_results", []))
        except Exception as exc:
            logger.debug("BB2 record_turn failed: %s", exc)
    else:
        try:
            _bb2_record_turn(query, local_results.get("raw_results", []))
        except Exception as exc:
            logger.debug("BB2 record_turn failed: %s", exc)

    if local_results["output"]:
        _record_spaced_repetition(
            local_db if local_db.exists() else global_db,
            local_results.get("results", []),
            query,
        )
        return (
            (resolution_note + str(local_results["output"]))
            if resolution_note
            else str(local_results["output"])
        )
    _record_spaced_repetition(
        local_db if local_db.exists() else global_db,
        [],
        query,
    )
    return resolution_note + "No memories matched the query."


@mcp.tool()
@with_audit("memory_semantic_search")
def memory_semantic_search(query: str, limit: int = 5) -> str:
    """Semantic search using embeddings alongside FTS5."""
    from mcp_common import GLOBAL_SCRIPTS_DIR

    script = GLOBAL_SCRIPTS_DIR / "embedding_search.py"
    if not script.exists():
        return _err(ErrorCode.NOT_FOUND, f"embedding_search.py not found at {script}")
    active = _resolve_memory_dir()
    out, _ = _run_subprocess_output(
        [sys.executable, str(script), query, str(limit)],
        timeout=30,
        cwd=str(active),
    )
    return out


@mcp.tool()
@with_audit("memory_recall_context")
def memory_recall_context(
    query: str = "",
    limit: int = 15,
    include_pinned: bool = True,
    include_recent_digests: bool = True,
    include_high_importance: bool = True,
    include_user_profile: bool = True,
    days_recent: int = 7,
    deep_rerank: bool = False,
) -> str:
    """Assemble a structured memory recall briefing for agent cold-start or session continuity.

    deep_rerank: when True, runs the Qwen3-0.6B / BGE-m3 deep reranker on
    the relevant-memories section (1-5s extra CPU, best ranking quality).
    Default False so the briefing is bounded to <100ms. ON APPLE SILICON
    (MPS) the deep reranker can hang indefinitely in a PyTorch MPS kernel
    (2026-06-19 incident: PIDs 68335, 10086). If you don't need the
    best-quality ranking, leave this False. If you do need it and the call
    hangs, set the MEMORY_RERANKER_DISABLED env var or
    `reranker_disabled = true` in memory.toml to fully disable the
    reranker (falls back to the lightweight weak cross-encoder).
    """
    from recall import recall_context

    target_base = _resolve_memory_dir()
    db_path = target_base / "memory.db"
    if not db_path.exists():
        return _err(ErrorCode.DB_ERROR, f"No memory.db at {db_path}")
    try:
        result = recall_context(
            db_path=str(db_path),
            query=query,
            limit=limit,
            include_pinned=include_pinned,
            include_recent_digests=include_recent_digests,
            include_high_importance=include_high_importance,
            include_user_profile=include_user_profile,
            days_recent=days_recent,
            deep_rerank=deep_rerank,
        )
        return cast(str, result.get("formatted", "No recall available."))
    except Exception as e:
        logger.exception("Recall failed")
        return _err(ErrorCode.RECALL_ERROR, "Recall failed")


@mcp.tool()
@with_audit("memory_session_start")
def memory_session_start(query: str = "") -> str:
    """Retrieve the session startup briefing including recent stats, recall context, and spaced repetition review schedule.

    USE THIS TOOL WHEN:
    - You start a new interaction session with the user.
    - You want a quick summary of the current memory database statistics, active memory tiers, and any pending spaced-repetition review items.

    ARGUMENTS:
    - query: An optional query to scope the briefing to (e.g., active topic or project).

    RETURNS:
    A human-readable text briefing with database statistics and session context.
    """
    from recall import recall_context
    from self_directed import SELF_DIRECTED_ENABLED
    from mcp_common import GLOBAL_SCRIPTS_DIR

    target_base = _resolve_memory_dir()
    db_path = target_base / "memory.db"
    if not db_path.exists():
        return _err(ErrorCode.DB_ERROR, f"No memory.db at {db_path}")
    try:
        recall = recall_context(
            db_path=str(db_path),
            query=query,
            limit=15,
            include_pinned=True,
            include_recent_digests=True,
            include_high_importance=True,
            include_user_profile=True,
            days_recent=7,
        )
        briefing = recall.get("formatted", "No recall available.")

        stats_section = ""
        if SELF_DIRECTED_ENABLED:
            try:
                from self_directed import tier_stats_db

                stats = tier_stats_db(db_path)
                total = stats.get("total", 0)
                pinned = stats.get("pinned", 0)
                stats_section = (
                    f"\n**Database Stats**: {total} memories, {pinned} pinned\n"
                )
                for tier, info in stats.get("tiers", {}).items():
                    stats_section += f"  {tier}: {info['count']} (avg importance={info['avg_importance']:.2f})\n"
            except Exception:
                stats_section = ""
        else:
            total = recall.get("total_memories", 0)
            stats_section = f"\n**Database Stats**: {total} memories\n"

        review_section = ""
        try:
            script = GLOBAL_SCRIPTS_DIR / "spaced_repetition.py"
            if script.exists():
                out, _ = _run_subprocess_output(
                    [sys.executable, str(script)], timeout=10, cwd=str(target_base)
                )
                if out and not out.startswith("[stderr]"):
                    review_section = f"\n**Review Schedule**:\n{out}\n"
        except Exception:
            pass

        return f"{briefing}\n{stats_section}{review_section}"
    except Exception as e:
        logger.exception("Session start failed")
        return _err(ErrorCode.SESSION_START_ERROR, "Session start failed")
