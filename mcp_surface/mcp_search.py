from __future__ import annotations

"""
Search MCP tools — memory_search, memory_semantic_search, memory_recall_context, memory_session_start.
"""

import os
import sys
import json
import logging
from pathlib import Path


from typing import Any, cast

from mcp_surface.mcp_common import (
    _resolve_memory_dir,
    _run_subprocess_output,
    GLOBAL_SCRIPTS_DIR,
    GLOBAL_MEM_DIR,
    get_memory_paths,
    logger,
    _err,
    ErrorCode,
    with_audit,
)
from mcp_surface.mcp_instance import mcp
from search_pipeline import search_memories as _search_memories_impl
from search_pipeline import _bb2_resolve, _bb2_record_turn

logger = logging.getLogger(__name__)

# B1 fix (2026-06-22): alias the canonical implementation so
# ``from mcp_surface.mcp_search import search_memories`` resolves to the same
# function object as ``from search_pipeline import search_memories``.
# Earlier this module re-implemented the wrapper in-place (with the
# same signature), which silently shadowed any monkey-patch applied to
# the canonical function. The two definitions were identical but the
# identity wasn't shared, breaking ``is`` checks in tests.
search_memories = _search_memories_impl


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
        # Best-effort: bound the write-session wait so search is never
        # stalled by a contended writer in another process.
        sr = _SpacedRepetition(db_path, session_timeout=0.5)
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
# ``from mcp_surface.mcp_search import search_memories`` and
# ``from search_pipeline import search_memories`` resolve to the same
# function object. Earlier this was a fresh function with the same
# signature, so identity checks (``is``) in tests silently failed and
# monkey-patches applied to the canonical function were bypassed.


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
    tenant_id: str = "default",
    as_of: float | None = None,
    belief_status: str | None = None,
    epistemic_source: str | None = None,
    fact_type: str | None = None,
    memory_source: str | None = None,
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
    - as_of: If set, search as of this epoch timestamp (time-travel query). Only memories valid at this time are returned. Default is None (current time).
    - belief_status: Optional filter — only return facts with this belief status (active, retracted, deprecated, unconfirmed).
    - epistemic_source: Optional filter — only return facts from this source (agent, auto_save, hook, import, cron).
    - fact_type: Optional filter — only return facts of this type (observation, agent_inference, external_stated, hypothesis, derived).
    - memory_source: Filter memories by source type ("agent", "auto_save", "import"). Only returns memories whose source file category matches the given type.

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
    logger.warning(
        "MCP_SEARCH: query=%r include_global=%s tenant_id=%s",
        query,
        include_global,
        tenant_id,
    )
    if local_db.exists():
        try:
            local_results = search_memories(
                local_db,
                expanded_query,
                limit,
                include_global=include_global,
                rerank=rerank,
                boost_pinned=boost_pinned,
                recency_weight=recency_weight,
                include_invalid=include_invalid,
                deep_rerank=deep_rerank,
                include_facts=include_facts,
                fact_limit=fact_limit,
                tenant_id=tenant_id,
                as_of=as_of,
                belief_status=belief_status,
                epistemic_source=epistemic_source,
                fact_type=fact_type,
                memory_source=memory_source,
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
                tenant_id="default",
                as_of=as_of,
                belief_status=belief_status,
                epistemic_source=epistemic_source,
                fact_type=fact_type,
                memory_source=memory_source,
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

    # Clamp query length to prevent arg overflow / resource exhaustion
    MAX_QUERY_LEN = 4096
    if len(query) > MAX_QUERY_LEN:
        query = query[:MAX_QUERY_LEN]

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
    session_id: str = "",
    limit: int = 15,
    include_pinned: bool = True,
    include_recent_digests: bool = True,
    include_high_importance: bool = True,
    include_user_profile: bool = True,
    days_recent: int = 7,
    deep_rerank: bool = False,
    action: str | None = None,
    include_global: bool = True,
    **kwargs: Any,
) -> str:
    """Assemble a structured memory recall briefing for agent cold-start or session continuity.

    deep_rerank: when True, runs the Qwen3-0.6B / BGE-m3 deep reranker on
    the relevant-memories section (1-5s extra CPU, best ranking quality).
    Default False so the briefing is bounded to <100ms. On Apple Silicon
    (MPS) the deep reranker is auto-disabled by default — a PyTorch MPS
    kernel can hang the process indefinitely (2026-06-19 incident: PIDs
    68335, 10086) — and the call falls back to the lightweight weak
    cross-encoder. Opt in with the env var MEMORY_RERANKER_MPS_ENABLED=1;
    MEMORY_RERANKER_DISABLED / `reranker_disabled = true` in memory.toml
    still fully disable the reranker everywhere.
    """
    from recall.recall import recall_context

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
    except Exception:
        logger.exception("Recall failed")
        return _err(ErrorCode.RECALL_ERROR, "Recall failed")


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
    from infra.memory_common import is_session_active

    if is_session_active(max_age_seconds=3600):
        return (
            "Session already initialized. Use memory_search or memory_save to continue."
        )

    target_base = _resolve_memory_dir()
    db_path = target_base / "memory.db"
    if not db_path.exists():
        return _err(ErrorCode.DB_ERROR, f"No memory.db at {db_path}")

    # Anchor the session if no session is currently active, so that
    # memory_session_end has an authoritative handle later, even when the
    # harness session-start hook never ran. Mirrors the hook's state-file
    # schema (session_id + started_at) — see hooks/memory-session-start.py.
    try:
        import time as _time

        from infra.memory_common import get_sessions_dir

        sessions_dir = get_sessions_dir()
        sessions_dir.mkdir(parents=True, exist_ok=True)
        state_file = sessions_dir / ".current_session.json"
        existing_state: dict = {}
        if state_file.exists():
            try:
                existing_state = json.loads(state_file.read_text())
            except Exception:
                existing_state = {}
        if not existing_state.get("session_id"):
            from session_manager import SessionManager

            mgr = (
                SessionManager(db_path=db_path)
                if db_path.exists()
                else SessionManager()
            )
            ctx = mgr.start_session(project_root=str(Path.cwd()))
            if ctx is not None:
                existing_state["session_id"] = ctx.session.id
        existing_state.setdefault("started_at", _time.time())
        existing_state.setdefault(
            "started_iso", _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
        )
        existing_state.setdefault("source", "memory_session_start_verb")
        state_file.write_text(json.dumps(existing_state, indent=2))
    except Exception as _ss_exc:
        logger.warning("memory_session_start: could not anchor session: %s", _ss_exc)

    from recall.recall import recall_context
    from self_directed import SELF_DIRECTED_ENABLED

    try:
        embedding_status = ""
        if query:
            try:
                from infra.embedding_search import get_embedding_search

                es = get_embedding_search()
                if es._model_load_failed:
                    embedding_status = (
                        "\n**⚠️  Embedding model failed to load.** "
                        "Semantic search is degraded to FTS5-only mode."
                    )
                elif not es._model_loaded:
                    embedding_status = (
                        "\n**⚠️  Embedding model loading…** "
                        "Semantic search not yet available; using FTS5."
                    )
            except Exception:
                pass
        recall = recall_context(
            db_path=str(db_path),
            query=query,
            limit=15,
            include_pinned=True,
            include_recent_digests=True,
            include_high_importance=True,
            include_user_profile=True,
            days_recent=7,
            fts_relevance=True,
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
            import datetime as _dt
            from infra.db import open_db

            with open_db(db_path, timeout=5.0, pooled=True, write=False) as rconn:
                today = _dt.date.today().isoformat()
                row_total = rconn.execute(
                    "SELECT COUNT(*) FROM review_schedule"
                ).fetchone()
                row_due = rconn.execute(
                    "SELECT COUNT(*) FROM review_schedule WHERE next_review <= ?",
                    (today,),
                ).fetchone()
                if row_total and row_due:
                    review_section = f"\n**Review Schedule**:\n  Total scheduled: {row_total[0]}\n  Due for review: {row_due[0]}\n"
        except Exception:
            pass

        return f"{briefing}{embedding_status}\n{stats_section}{review_section}"
    except Exception:
        logger.exception("Session start failed")
        return _err(ErrorCode.SESSION_START_ERROR, "Session start failed")


# Legacy alias for backward compatibility with older test suites
memory_recall_stats = memory_recall_context
