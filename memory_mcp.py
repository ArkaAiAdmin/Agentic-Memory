#!/usr/bin/env python3
import _bootstrap_path  # noqa: E402
import os
import signal
import sys
from pathlib import Path

import json
import logging
from datetime import datetime
import sqlite3
import subprocess
import re
import math
import time
import shutil
import unicodedata
from typing import Optional
from mcp_instance import mcp  # noqa: E402 — shared instance, avoids circular import
from memory_common import (
    parse_frontmatter,
    atomic_write,
    configure_logging,
    open_db,
    count_rows,
    safe_call,
    acquire_flock_with_retry,
    release_flock,
    rate_limit_check,
    run_db_migrations,
    safe_close_db,
    connection_pool,
)  # noqa: E402
from infrastructure import (  # noqa: E402
    _normalize_unicode,
    _resolve_active_db_path,
    _try_extract_result_meta,
    with_audit,
    _err,
    resolve_active_memory_dir,
    resolve_db_for_memory_id,
    add_link_to_memory_md_content,
    update_memory_md_locked,
    GLOBAL_MEM_DIR,
)
from search_pipeline import (  # noqa: E402
    _RERANK_WEIGHTS,
    _RERANK_HALF_LIFE_DAYS,
    _RERANK_TOKEN_RE,
    _QUERY_EXPANSIONS,
    _QUERY_EXPANSION_REVERSE,
    _RRF_K,
    _QUERY_TYPE_TEMPORAL_RE,
    _QUERY_TYPE_MULTIHOP_RE,
    _QUERY_TYPE_CODE_RE,
    _QUERY_TYPE_FACTUAL_RE,
    _expand_query,
    _did_you_mean,
    _top_recent_tags,
    _top_recent_notes,
    _top_recent_source_files,
    _build_zero_result_suggestions,
    _detect_query_type,
    _weights_for_query_type,
    _reciprocal_rank_fusion,
    _tokenize_for_ce,
    _cross_encoder_score,
    _apply_cross_encoder_rerank,
    _qw5_extract_keywords,
    _qw5_keyword_similarity,
    _qw5_is_topic_boundary,
    _qw5_chunk_content,
    _qw5_ensure_schema,
    _qw5_index_chunks_for,
    _merge_chunk_hits,
    _search_chunks_enhanced,
    _late_interaction_score,
    _apply_late_interaction_rerank,
    _temporal_decay_factor,
    _apply_temporal_decay,
    _bb1_split_sentences,
    _bb1_synthesize,
    _compute_final_score,
    _bb2_extract_terms,
    _bb2_is_reference_query,
    _bb2_resolve,
    _bb2_record_turn,
    _bb2_clear_history,
    _graph_rag_expand,
    search_memories as _search_pipeline_search_memories,
)
import search_pipeline  # noqa: E402
from save_pipeline import (  # noqa: E402
    _update_memory_index_incremental,
    _recalculate_fitness_scores,
    _auto_backlink_multi_part,
    save_memory,
)
from mcp_tools import (  # noqa: E402
    search_memories as _search_memories_reexport,
    memory_search,
    memory_save,
    memory_graph_search,
    memory_graph_stats,
    memory_facts_search,
    memory_facts_list,
    memory_facts_stats,
    memory_heartbeat,
    memory_tier_stats,
    memory_duplicates,
    memory_merge_suggestions,
    memory_supersede,
    memory_auto_save_hook,
    memory_daily_digest,
    memory_auto_save_status,
    memory_rebuild,
    memory_reinforce,
    memory_compile_skill,
    memory_audit,
    _run_subprocess_output,
    memory_audit_query,
    memory_consolidate,
    memory_rewrite_links,
    memory_detect_contradictions,
    memory_semantic_search,
    memory_compact,
    memory_arc_stats,
    memory_review_schedule,
    memory_pinned_decay_check,
    recompile_skills_catalog,
    memory_delete,
    memory_restore,
    memory_trash,
    memory_purge_expired,
    memory_purge_auto_saves,
    memory_scan_injection,
    memory_strip_provenance,
    memory_check_integrity,
    memory_check_contradictions,
    memory_quality_filter,
    memory_quality_stats,
    memory_summarize,
    memory_auto_summarize,
    memory_summarization_stats,
    memory_profile_access,
    memory_user_profile,
    memory_profile_stats,
    memory_adaptive_retention,
    memory_retention_stats,
    memory_share,
    memory_shared_list,
    memory_shared_import,
    memory_shared_stats,
    memory_backfill_all,
)

# Backward-compatible aliases: tests and external code may access
# memory_mcp._BB2_TURNS, memory_mcp._TEMPORAL_DECAY_MODE, etc.
#
# These are snapshot copies (bound at module load time), not live
# links. If the value in search_pipeline is later reassigned,
# memory_mcp will still hold the original object.  This is safe in
# practice because none of these names are reassigned after import,
# but it is a deliberate tradeoff: deep proxying would add runtime
# overhead for no observed benefit. (Y4 audit finding, 2026-06-20)
_BB2_TURNS = search_pipeline._BB2_TURNS
_TEMPORAL_DECAY_MODE = search_pipeline._TEMPORAL_DECAY_MODE
_QUERY_TYPE_WEIGHTS = search_pipeline._QUERY_TYPE_WEIGHTS
_QW5_CHUNK_THRESHOLD = search_pipeline._QW5_CHUNK_THRESHOLD
_QW5_CHUNK_TARGET_SIZE = search_pipeline._QW5_CHUNK_TARGET_SIZE
_RRF_K = search_pipeline._RRF_K
_CROSS_ENCODER_BLEND = search_pipeline._CROSS_ENCODER_BLEND
_CE_STOPWORDS = search_pipeline._CE_STOPWORDS
_LATE_INTERACTION_ENABLED = search_pipeline._LATE_INTERACTION_ENABLED
_LATE_INTERACTION_BLEND = search_pipeline._LATE_INTERACTION_BLEND
_QUERY_EXPANSIONS = search_pipeline._QUERY_EXPANSIONS
_QUERY_EXPANSION_REVERSE = search_pipeline._QUERY_EXPANSION_REVERSE

# H1 fix: configure root logging once at module load (idempotent).
configure_logging()
logger = logging.getLogger(__name__)
# The agentic-memory project is special: scripts live at the top level of
# ~/.config/agentic-memory/, but the actual global memories live one level
# deeper at ~/.config/agentic-memory/memory/. Fixing C1.
GLOBAL_SCRIPTS_DIR = Path.home() / ".config" / "agentic-memory"

# H1-b fix: the canonical find_project_root lives in memory_common.py. Import
# it instead of duplicating the marker list here (the two implementations had
# drifted).
sys.path.insert(0, str(GLOBAL_SCRIPTS_DIR))
from memory_common import find_project_root, get_memory_paths  # noqa: E402
import audit  # noqa: E402

# --- Cache (imported from cache.py, aliased to old underscore names) ---
import cache as _cache_mod

_search_cache = _cache_mod._search_cache
_SEARCH_CACHE_MAX = _cache_mod.SEARCH_CACHE_MAX
_SEARCH_CACHE_QUERY_MAX = 256  # not imported; only used in make_cache_key
_SEARCH_CACHE_TTL = _cache_mod.SEARCH_CACHE_TTL
_SEARCH_CACHE_TTL_ENABLED = _cache_mod.SEARCH_CACHE_TTL_ENABLED
safety_wiring = _cache_mod.safety_wiring
_make_cache_key = _cache_mod.make_cache_key  # backward compat alias
cache_stats = _cache_mod.cache_stats  # backward compat re-export

# Public aliases — tests and external code import these directly from memory_mcp
#
# B1 / B2 fix (2026-06-22): these are intentional public-API aliases for
# backward compatibility, NOT independent re-implementations.
#
#   * ``search_memories`` is an alias of ``search_pipeline.search_memories``,
#     which itself is a re-export of ``search.orchestrator.search_memories``
#     (the canonical implementation).  ``from memory_mcp import
#     search_memories`` and ``from search_pipeline import search_memories``
#     resolve to the same function object, so ``is`` checks pass and
#     monkey-patches are not silently bypassed.
#   * ``save_memory`` is the same function object that
#     ``save_pipeline.save_memory`` exports.  Older callers that did
#     ``from memory_mcp import save_memory`` (before the 2026-06-20
#     save_pipeline extraction) keep working.
#
# If you find yourself wanting to add a third re-implementation, route it
# through these aliases instead so identity is preserved.
search_memories = _search_pipeline_search_memories
save_memory = save_memory


# ---------------------------------------------------------------------
# Feature D: Async Pipeline (extracted to mcp_async.py)
# ---------------------------------------------------------------------
from mcp_async import (  # noqa: E402
    async_memory_save,
    async_memory_search,
    async_memory_save_batch,
    async_memory_search_batch,
)


# --- Tool Registry Filtering ---
# Remove admin tools from MCP surface; they're accessible via memory_maintenance.
import tool_registry  # noqa: E402

# Keep memory_maintenance (the router) visible; hide individual admin tools.
for _admin_name in tool_registry.ADMIN_TOOLS:
    if _admin_name == "memory_maintenance":
        continue
    try:
        mcp.remove_tool(_admin_name)
    except Exception:
        pass  # tool not registered yet, or FastMCP version doesn't support it


if __name__ == "__main__":
    # Start the optional CRDT sync server as a daemon thread.
    try:
        from sync_server import start_server_from_config
        from infrastructure import resolve_active_memory_dir

        active_dir = resolve_active_memory_dir()
        start_server_from_config(active_dir / "memory.db")
    except Exception as e:
        logger.info("sync server not started: %s", e)

    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    try:
        mcp.run()
    except (BrokenPipeError, OSError, EOFError):
        pass  # parent closed stdio — expected during restart
