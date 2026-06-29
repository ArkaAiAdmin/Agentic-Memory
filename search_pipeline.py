"""Search pipeline for the agentic-memory system.

Extracted from memory_mcp.py to separate search logic from the MCP server
interface. Contains query expansion, reranking, cross-encoder scoring,
chunk-based search, temporal decay, conversation history resolution,
Graph-RAG expansion, and the main search_memories orchestrator.
"""

# ---------------------------------------------------------------------------
# Backward-compat module attribute proxying
#
# After extracting logic to the ``search.*`` subpackage, some test suites
# (and external callers) reset module-level state via direct attribute
# assignment, e.g. ``import search_pipeline as sp; sp._CTR_WEIGHTS_CACHE = None``.
# That pattern used to mutate the live cache directly.  Now that the cache
# lives in ``search.scoring``, a plain assignment on this module would
# shadow the re-exported binding without actually clearing the live cache.
#
# We solve this with two cooperating hooks (see ``_ProxyModule`` below):
#
#   * ``__getattr__`` (line 1858) — handles lazy config-flag lookups
#     when a name is not present in the module namespace (e.g.
#     ``_TEMPORAL_DECAY_HALF_LIFE`` which is resolved from
#     ``memory.toml`` via ``config.get_config()``). This is standard
#     module-level ``__getattr__`` and only fires for MISSING names.
#
#   * ``__setattr__`` (via ``_ProxyModule``) — intercepts ALL writes
#     on this module. For the curated keys in ``_proxy_targets``
#     (currently only ``_CTR_WEIGHTS_CACHE``), the write is forwarded
#     to the submodule that owns the canonical copy. For all other
#     names, the write falls through to normal ``super().__setattr__``,
#     which populates this module's ``__dict__`` directly.
#
# These two hooks coexist correctly because ``__getattr__`` is only
# consulted AFTER ``__getattribute__`` returns ``AttributeError`` —
# they never conflict. Do not remove either hook without adding a
# comment explaining why the other one is not affected.
# ---------------------------------------------------------------------------
# Canonical __getattr__ lives in search/__init__.py (single source of
# truth for both the search_memories proxy and config lazy keys).
from search import __getattr__ as _search_getattr  # noqa: E402

# Proxy class + _install_proxies: forward writes to _CTR_WEIGHTS_CACHE
# (and similar shared state) to the canonical submodule. This is what
# lets tests do `search_pipeline._CTR_WEIGHTS_CACHE = None` and have
# it actually clear the live cache in search.scoring.
import sys as _sys
import types as _types

_proxy_targets: dict = {}


def _install_proxies() -> None:
    """Lazily wire up attribute forwarding for known shared state."""
    global _proxy_targets
    if _proxy_targets:
        return
    try:
        from search import scoring as _scoring
    except Exception:
        return
    _proxy_targets = {
        "_CTR_WEIGHTS_CACHE": _scoring,
    }


class _ProxyModule(_types.ModuleType):
    """Module subclass that forwards writes to curated names to their canonical home."""

    def __setattr__(self, name: str, value) -> None:
        if not _proxy_targets:
            _install_proxies()
        if name in _proxy_targets:
            setattr(_proxy_targets[name], name, value)
            return
        super().__setattr__(name, value)


_module = _sys.modules[__name__]
_module.__class__ = _ProxyModule


# Install the canonical __getattr__ on this module so that
# ``from search_pipeline import _TEMPORAL_DECAY_HALF_LIFE`` etc. still
# resolves through the same code path as ``from search import ...``.
_module.__getattr__ = _search_getattr  # type: ignore[method-assign]


__all__ = [
    "search_memories",
    "compute_channel_weights",
    # query_parser (source-of-truth: search/query_parser.py — Y1/Y2)
    "_detect_query_type",
    "_weights_for_query_type",
    "_expand_query",
    "_did_you_mean",
    "_graph_rag_expand",
    "_top_recent_tags",
    "_top_recent_notes",
    "_top_recent_source_files",
    "_build_zero_result_suggestions",
    "_QUERY_EXPANSIONS",
    "_QUERY_EXPANSION_REVERSE",
    "_QUERY_TYPE_WEIGHTS",
    "_QUERY_TYPE_TEMPORAL_RE",
    "_QUERY_TYPE_MULTIHOP_RE",
    "_QUERY_TYPE_CODE_RE",
    "_QUERY_TYPE_FACTUAL_RE",
    # rerankers (source-of-truth: search/rerankers.py)
    "_tokenize_for_ce",
    "_cross_encoder_score",
    "_apply_cross_encoder_rerank",
    "_late_interaction_score",
    "_precompute_query_ngrams",
    "_late_interaction_score_batch",
    "_CE_STOPWORDS",
    "_CROSS_ENCODER_BLEND",
    "_LATE_INTERACTION_BLEND",
    # scoring (source-of-truth: search/scoring.py)
    "_reciprocal_rank_fusion",
    "_temporal_decay_factor",
    "_apply_temporal_decay",
    "_apply_neural_forget_curve",
    "_strong_match_float",
    "_compute_final_score",
    "_RRF_K",
    "_RERANK_WEIGHTS",
    "_RERANK_HALF_LIFE_DAYS",
    "_RERANK_TOKEN_RE",
    "_STRONG_BM25_THRESHOLD",
    "_CTR_WEIGHTS_CACHE",
    # synthesis (source-of-truth: search/synthesis.py)
    "_bb1_split_sentences",
    "_bb1_synthesize",
    "_bb2_extract_terms",
    "_bb2_is_reference_query",
    "_bb2_resolve",
    "_bb2_record_turn",
    "_bb2_clear_history",
    "_BB1_SENT_SPLIT",
    "_BB1_DEFAULT_MAX_SENTENCES",
    "_BB1_CONTEXT_SENTENCES",
    "_BB2_TURNS",
    "_BB2_LOCK",
    "_BB2_HISTORY_MAX",
    "_BB2_PRONOUNS",
    "_BB2_REF_PHRASES",
    "_BB2_STOPWORDS",
    # chunk_index (source-of-truth: search/chunk_index.py)
    "_qw5_extract_keywords",
    "_qw5_keyword_similarity",
    "_qw5_is_topic_boundary",
    "_qw5_chunk_content",
    "_qw5_ensure_schema",
    "_qw5_index_chunks_for",
    "_QW5_CHUNK_THRESHOLD",
    "_QW5_CHUNK_TARGET_SIZE",
    "_QW5_CHUNK_OVERLAP",
    "_QW5_CHUNK_MAX_SIZE",
    "_QW5_TOPIC_SIMILARITY_THRESHOLD",
    "_QW5_SENT_BOUNDARY",
    "_QW5_STOPWORDS",
    "_QW5_CHUNKS_SCHEMA_SQL",
    "_QW5_CHUNKS_TRIGGERS_SQL",
]

# ---------------------------------------------------------------------------
# Cross-encoder + late-interaction rerank primitives
#
# Extracted to search.rerankers (2026-06-20). The inline definitions
# below are kept for backward compat; the import shim makes the new
# module the source of truth. Once the inline defs are removed, the
# rerankers live entirely in search/rerankers.py.
# ---------------------------------------------------------------------------
from search.rerankers import (  # noqa: E402, F401
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

# ---------------------------------------------------------------------------
# Scoring / fusion / decay primitives
#
# Extracted to search.scoring (2026-06-20). The inline definitions
# below are kept for backward compat; the import shim makes the new
# module the source of truth. Once the inline defs are removed, the
# scoring functions live entirely in search/scoring.py.
# ---------------------------------------------------------------------------
from search.scoring import (  # noqa: E402, F401
    _reciprocal_rank_fusion,
    _temporal_decay_factor,
    _apply_temporal_decay,
    _apply_neural_forget_curve,
    _strong_match_float,
    _compute_final_score,
    compute_channel_weights,
    _CTR_WEIGHTS_CACHE,
    _RRF_K,
    _RERANK_WEIGHTS,
    _RERANK_HALF_LIFE_DAYS,
    _RERANK_TOKEN_RE,
    _STRONG_BM25_THRESHOLD,
)

# ---------------------------------------------------------------------------
# BB1 / BB2 synthesis primitives
#
# Extracted to search.synthesis (2026-06-20). Re-exported here so
# existing callers using ``from search_pipeline import _bb1_synthesize``
# etc. keep working without modification. The BB2 turn-history state
# (_BB2_TURNS) is the same list object via both import paths.
# ---------------------------------------------------------------------------
from search.synthesis import (  # noqa: E402, F401
    _bb1_split_sentences,
    _bb1_synthesize,
    _bb2_extract_terms,
    _bb2_is_reference_query,
    _bb2_resolve,
    _bb2_record_turn,
    _bb2_clear_history,
    _BB1_SENT_SPLIT,
    _BB1_DEFAULT_MAX_SENTENCES,
    _BB1_CONTEXT_SENTENCES,
    _BB2_TURNS,
    _BB2_LOCK,
    _BB2_HISTORY_MAX,
    _BB2_PRONOUNS,
    _BB2_REF_PHRASES,
    _BB2_STOPWORDS,
)

# ---------------------------------------------------------------------------
# QW5 chunking primitives
#
# Extracted to search.chunk_index (2026-06-20). Re-exported here so
# existing callers using ``from search_pipeline import _qw5_chunk_content``
# etc. keep working without modification.
# ---------------------------------------------------------------------------
from search.chunk_index import (  # noqa: E402, F401
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

from search.query_parser import (
    _QUERY_EXPANSIONS,
    _QUERY_EXPANSION_REVERSE,
    _QUERY_TYPE_WEIGHTS,
    _QUERY_TYPE_TEMPORAL_RE,
    _QUERY_TYPE_MULTIHOP_RE,
    _QUERY_TYPE_CODE_RE,
    _QUERY_TYPE_FACTUAL_RE,
    _escape_fts_query,
    _escape_phrase,
    _expand_query,
    _did_you_mean,
    _top_recent_tags,
    _top_recent_notes,
    _top_recent_source_files,
    _build_zero_result_suggestions,
    _detect_query_type,
    _weights_for_query_type,
    _graph_rag_expand,
)

from search.orchestrator import (
    ScoreContext,
    _merge_chunk_hits,
    _fallback_embedding_search,
    check_concept_drift_db,
    search_memories,
)

from infra.memory_config import GLOBAL_MEM_DIR  # noqa: E402 — backward compat re-export


# (Moved to search/__init__.py — canonical location.)
# Marker remains for reset_all_lazy_config_attrs().


# Marker for reset_all_lazy_config_attrs(): this module uses a hand-rolled
# __getattr__ with underscore-prefixed lazy config keys.
_lazy_config_attr_names = frozenset(
    {
        "_LATE_INTERACTION_ENABLED",
        "_TEMPORAL_DECAY_HALF_LIFE",
        "_TEMPORAL_DECAY_MODE",
        "_FORGETTING_CURVE_ENABLED",
        "_FORGETTING_CURVE_HALF_LIFE",
        "_GRAPH_RAG_ENABLED",
        "_GRAPH_RAG_MAX_HOPS",
        "_GRAPH_RAG_MAX_EXPANSIONS",
        "_RERANK_HALF_LIFE_DAYS",
        "_TEMPORAL_DECAY_WEIGHT",
    }
)
