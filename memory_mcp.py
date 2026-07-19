#!/usr/bin/env python3
import os
import signal
import sys
from pathlib import Path

import logging
from mcp_instance import mcp  # noqa: E402 — shared instance, avoids circular import
from mcp_common import _bootstrap_path  # noqa: E402,F401
from infra.memory_common import (
    configure_logging,
)  # noqa: E402
from infra.infrastructure import (  # noqa: E402
    resolve_active_memory_dir,
)
from search_pipeline import (  # noqa: E402
    search_memories as _search_pipeline_search_memories,
)
import search_pipeline  # noqa: E402
from save_pipeline import (  # noqa: E402
    save_memory,
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

# Initialize agent context from MEMORY_AGENT_ID so RBAC resolves the
# correct principal instead of falling back to "default".
try:
    from agent_context import init_agent
    _agent_id = os.environ.get("MEMORY_AGENT_ID", "")
    if _agent_id:
        init_agent(_agent_id)
        logger.info("memory_mcp: initialized agent context for %s", _agent_id)
except Exception:
    pass

# Phase 4: configure per-tool rate limits from memory.toml [rate_limits].
try:
    from infra.rate_limiter import configure_rate_limits
    configure_rate_limits()
except Exception:
    logger.info("rate_limits not configured (no [rate_limits] section or import error)")
# The agentic-memory project is special: scripts live at the top level of
# ~/.config/agentic-memory/, but the actual global memories live one level
# deeper at ~/.config/agentic-memory/memory/. Fixing C1.
GLOBAL_SCRIPTS_DIR = Path.home() / ".config" / "agentic-memory"
from infra.memory_config import GLOBAL_MEM_DIR, get_memory_paths  # noqa: E402,F401

# H1-b fix: the canonical find_project_root lives in memory_common.py. Import
# it instead of duplicating the marker list here (the two implementations had
# drifted).
sys.path.insert(0, str(GLOBAL_SCRIPTS_DIR))

# --- Cache (imported from cache.py, aliased to old underscore names) ---
import infra.cache as _cache_mod

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

# Backward-compat re-exports of all MCP tool functions.
# Tests and external code import tool callables directly from memory_mcp.
from mcp_audit import (  # noqa: E402,F401
    memory_audit,
    memory_audit_query,
)
from mcp_memory import (  # noqa: E402,F401
    memory_auto_save_hook,
    memory_auto_save_status,
    memory_daily_digest,
    memory_delete,
    memory_reinforce,
    memory_restore,
    memory_save,
    memory_save as _mcp_memory_save_alias,
    memory_supersede,
    memory_trash,
)
from mcp_maintenance import (  # noqa: E402,F401
    memory_detect_contradictions,
    memory_pinned_decay_check,
    memory_review_schedule,
)
from mcp_search import (  # noqa: E402,F401
    memory_search,
)
from mcp_rebuild import (  # noqa: E402,F401
    memory_compact,
)
from mcp_summarization import (  # noqa: E402,F401
    memory_summarize,
)
from mcp_retention import (  # noqa: E402,F401
    memory_adaptive_retention,
)

# Backward-compat re-exports of internal search helpers.
# Tests and external code access these directly from memory_mcp.
from search.query_parser import (  # noqa: E402,F401
    _did_you_mean,
    _expand_query,
    _top_recent_tags,
    _top_recent_notes,
    _top_recent_source_files,
    _build_zero_result_suggestions,
    _detect_query_type,
    _weights_for_query_type,
    _graph_rag_expand,
)
from search.synthesis import (  # noqa: E402,F401
    _bb1_split_sentences,
    _bb1_synthesize,
    _BB1_SENT_SPLIT,
    _BB1_DEFAULT_MAX_SENTENCES,
    _BB1_CONTEXT_SENTENCES,
    _bb2_extract_terms,
    _bb2_is_reference_query,
    _bb2_resolve,
    _bb2_record_turn,
    _bb2_clear_history,
    _BB2_TURNS,
    _BB2_LOCK,
    _BB2_HISTORY_MAX,
    _BB2_PRONOUNS,
    _BB2_REF_PHRASES,
    _BB2_STOPWORDS,
)
from search.scoring import (  # noqa: E402,F401
    _reciprocal_rank_fusion,
    _RERANK_WEIGHTS,
    _temporal_decay_factor,
    _apply_temporal_decay,
    _apply_jaccard_surprise_penalty,
    _strong_match_float,
    _compute_final_score,
)
from search.rerankers import (  # noqa: E402,F401
    _tokenize_for_ce,
    _cross_encoder_score,
    _apply_cross_encoder_rerank,
    _late_interaction_score,
    _precompute_query_ngrams,
    _late_interaction_score_batch,
    _apply_late_interaction_rerank,
    _CE_STOPWORDS,
    _CROSS_ENCODER_BLEND,
    _LATE_INTERACTION_BLEND,
)
from search.chunk_index import (  # noqa: E402,F401
    _qw5_extract_keywords,
    _qw5_keyword_similarity,
    _qw5_is_topic_boundary,
    _qw5_chunk_content,
    _qw5_ensure_schema,
    _qw5_index_chunks_for,
    _QW5_CHUNK_THRESHOLD,
    _QW5_CHUNK_TARGET_SIZE,
    _QW5_CHUNK_OVERLAP,
    _QW5_CHUNK_MAX_SIZE,
    _QW5_TOPIC_SIMILARITY_THRESHOLD,
    _QW5_SENT_BOUNDARY,
    _QW5_STOPWORDS,
    _QW5_CHUNKS_SCHEMA_SQL,
    _QW5_CHUNKS_TRIGGERS_SQL,
)
from search.orchestrator import (  # noqa: E402,F401
    _merge_chunk_hits,
    _fallback_embedding_search,
)
from infra.infrastructure import with_audit  # noqa: E402,F401
from infra.memory_common import safe_close_db  # noqa: E402,F401


# ---------------------------------------------------------------------
# Feature D: Async Pipeline (extracted to mcp_async.py)
# ---------------------------------------------------------------------
from mcp_async import (  # noqa: E402,F401
    async_memory_save,
    async_memory_search,
    async_memory_save_batch,
    async_memory_search_batch,
)


# --- Tool Registry Filtering ---
# Remove admin tools from MCP surface; they're accessible via memory_maintenance.
import tool_registry  # noqa: E402

# Phase A: explicitly import the verb surface so tool registration is intentional.
import mcp_verbs  # noqa: E402, F401

# Import additional MCP modules so their tools are registered before the
# removal loop below — this prevents orphans appearing/disappearing
# depending on the entry point.
import mcp_agent  # noqa: E402,F401
import mcp_async  # noqa: E402,F401
import mcp_crdt  # noqa: E402,F401
import mcp_ctr_drift  # noqa: E402,F401
import mcp_dashboard  # noqa: E402,F401
import mcp_kg  # noqa: E402, F401 — graph_insights/evolution (ADMIN_TOOLS)
import mcp_kg_traversal  # noqa: E402,F401
import mcp_maintenance  # noqa: E402, F401 — memory_maintenance + admin tools
import mcp_maintenance_ops  # noqa: E402, F401
import mcp_metrics  # noqa: E402,F401
import mcp_multi_modal  # noqa: E402,F401
import mcp_okf  # noqa: E402,F401
import mcp_profile  # noqa: E402,F401
import mcp_quality  # noqa: E402,F401
import mcp_safety  # noqa: E402,F401
import mcp_sdk  # noqa: E402,F401
import mcp_session  # noqa: E402,F401
import mcp_sharing  # noqa: E402,F401
import mcp_health  # noqa: E402,F401

# Keep memory_maintenance (the router) visible; hide individual admin tools.
for _admin_name in tool_registry.ADMIN_TOOLS:
    if _admin_name == "memory_maintenance":
        continue
    try:
        mcp.remove_tool(_admin_name)
    except Exception as e:
        logger.warning("tool_registry vs registered-tools mismatch: cannot remove '%s' (%s)", _admin_name, e)


if __name__ == "__main__":
    # Parse --agent-id before singleton guard so the lock file is scoped
    # correctly even if the MCP client does not pass env vars.
    import argparse
    _parser = argparse.ArgumentParser()
    _parser.add_argument("--agent-id", default="", help="Set MEMORY_AGENT_ID")
    _args, _ = _parser.parse_known_args()
    if _args.agent_id:
        os.environ["MEMORY_AGENT_ID"] = _args.agent_id

    # Singleton guard: prevent duplicate MCP server instances on the same DB.
    try:
        from infra.mcp_singleton import acquire_mcp_singleton

        if not acquire_mcp_singleton():
            logger.error(
                "Another memory_mcp process is already running. "
                "Exiting to prevent flock contention. "
                "Kill the existing process or check for stale lock files in memory/"
            )
            sys.exit(1)
    except ImportError:
        logger.debug("mcp_singleton module not available, skipping singleton guard")

    # Start the optional CRDT sync server as a daemon thread.
    try:
        from infra.sync_server import start_server_from_config
        from infra.infrastructure import resolve_active_memory_dir

        active_dir = resolve_active_memory_dir()
        start_server_from_config(active_dir / "memory.db")
    except Exception as e:
        logger.info("sync server not started: %s", e)

    # Phase 2: auto-start the CQRS write-journal reconciler when enabled.
    try:
        from config import get_config
        _cfg = get_config()
        if getattr(_cfg, "write_journal", False):
            from background.background_worker import _start_reconciler
            from infra.write_journal import reset_stuck_processing
            _target_base = resolve_active_memory_dir()
            _journal_path = _target_base / "journal.db"
            # Stuck-entry self-heal: unstick entries from prior crashes.
            if _journal_path.exists():
                _unstuck = reset_stuck_processing(_journal_path)
                if _unstuck:
                    logger.info("write_journal: unstuck %d entries at startup", _unstuck)
            _start_reconciler(_journal_path, _target_base)
            logger.info(
                "write_journal reconciler: auto-started (journal=%s)", _journal_path
            )
    except Exception as e:
        logger.warning("write_journal reconciler: auto-start skipped: %s", e)

    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    try:
        mcp.run()
    except (BrokenPipeError, OSError, EOFError):
        pass  # parent closed stdio — expected during restart
