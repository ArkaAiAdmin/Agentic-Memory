"""Search pipeline subpackage.

Extracted from the original monolithic search_pipeline.py (2026-06-20)
as part of the god-module decomposition. The full search_pipeline.py
re-exports the symbols here for backward compatibility — existing
callers that do ``from search_pipeline import _parse_search_query``
keep working unchanged.
"""

from search.query_parser import (  # noqa: F401
    _parse_search_query,
    _escape_fts_query,
    _escape_phrase,
    _expand_query,
    _did_you_mean,
    _detect_query_type,
    _weights_for_query_type,
    _graph_rag_expand,
    _top_recent_tags,
    _top_recent_notes,
    _top_recent_source_files,
    _build_zero_result_suggestions,
)
from search.rerankers import (  # noqa: F401
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
from search.scoring import (  # noqa: F401
    _reciprocal_rank_fusion,
    _temporal_decay_factor,
    _apply_temporal_decay,
    _apply_neural_forget_curve,
    _strong_match_float,
    _compute_final_score,
    compute_channel_weights,
    _RRF_K,
    _RERANK_WEIGHTS,
    _RERANK_HALF_LIFE_DAYS,
    _RERANK_TOKEN_RE,
    _STRONG_BM25_THRESHOLD,
)
from search.synthesis import (  # noqa: F401
    _bb1_split_sentences,
    _bb1_synthesize,
    _bb2_extract_terms,
    _bb2_is_reference_query,
    _bb2_resolve,
    _bb2_record_turn,
    _bb2_clear_history,
    _BB2_TURNS,
    _BB2_LOCK,
    _BB2_HISTORY_MAX,
)
from search.chunk_index import (  # noqa: F401
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
)
from search.orchestrator import (  # noqa: F401
    ScoreContext,
    _merge_chunk_hits,
    _fallback_embedding_search,
)

__all__ = [
    # query_parser
    "_parse_search_query",
    "_escape_fts_query",
    "_escape_phrase",
    "_expand_query",
    "_did_you_mean",
    "_detect_query_type",
    "_weights_for_query_type",
    "_graph_rag_expand",
    "_top_recent_tags",
    "_top_recent_notes",
    "_top_recent_source_files",
    "_build_zero_result_suggestions",
    # rerankers
    "_tokenize_for_ce",
    "_cross_encoder_score",
    "_apply_cross_encoder_rerank",
    "_late_interaction_score",
    "_precompute_query_ngrams",
    "_late_interaction_score_batch",
    "_apply_late_interaction_rerank",
    "_CE_STOPWORDS",
    "_CROSS_ENCODER_BLEND",
    "_LATE_INTERACTION_BLEND",
    # scoring
    "_reciprocal_rank_fusion",
    "_temporal_decay_factor",
    "_apply_temporal_decay",
    "_apply_neural_forget_curve",
    "_strong_match_float",
    "_compute_final_score",
    "compute_channel_weights",
    "_RRF_K",
    "_RERANK_WEIGHTS",
    "_RERANK_HALF_LIFE_DAYS",
    "_RERANK_TOKEN_RE",
    "_STRONG_BM25_THRESHOLD",
    # synthesis
    "_bb1_split_sentences",
    "_bb1_synthesize",
    "_bb2_extract_terms",
    "_bb2_is_reference_query",
    "_bb2_resolve",
    "_bb2_record_turn",
    "_bb2_clear_history",
    "_BB2_TURNS",
    "_BB2_LOCK",
    "_BB2_HISTORY_MAX",
    # chunk_index
    "_qw5_extract_keywords",
    "_qw5_keyword_similarity",
    "_qw5_is_topic_boundary",
    "_qw5_chunk_content",
    "_qw5_ensure_schema",
    "_qw5_index_chunks_for",
    # Orchestrator
    "ScoreContext",
    "_merge_chunk_hits",
    "_fallback_embedding_search",
    # Orchestrator — defined in search_pipeline shim, proxied here
    # via __getattr__ so that ``from search import search_memories``
    # works for users who import the subpackage directly.
    "search_memories",
]


def __getattr__(name: str):
    if name == "ScoreContext":
        from search.orchestrator import ScoreContext
        return ScoreContext
    if name == "search_memories":
        from search.orchestrator import search_memories
        return search_memories
    if name in _SEARCH_LAZY_CONFIG_KEYS:
        from _lazy_imports import get_config

        spec = _SEARCH_LAZY_CONFIG_KEYS[name]
        if isinstance(spec, tuple):
            attr_name, transform = spec
            value = getattr(get_config(), attr_name)
            if callable(transform):
                value = transform(value)
        else:
            value = getattr(get_config(), spec)
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Config keys lazily resolved by this module's __getattr__.
# Paired with _lazy_config_attr_names in search_pipeline.py so that
# reset_all_lazy_config_attrs() finds and clears both modules.
_SEARCH_LAZY_CONFIG_KEYS: dict = {
    "_LATE_INTERACTION_ENABLED": "late_interaction",
    "_TEMPORAL_DECAY_HALF_LIFE": ("temporal_half_life", float),
    "_TEMPORAL_DECAY_MODE": "temporal_decay_mode",
    "_FORGETTING_CURVE_ENABLED": "forgetting_curve",
    "_FORGETTING_CURVE_HALF_LIFE": ("forgetting_curve_half_life", float),
    "_GRAPH_RAG_ENABLED": "knowledge_graph",
    "_GRAPH_RAG_MAX_HOPS": "graph_rag_hops",
    "_GRAPH_RAG_MAX_EXPANSIONS": "graph_rag_expansions",
    "_RERANK_HALF_LIFE_DAYS": ("rerank_half_life_days", float),
    "_TEMPORAL_DECAY_WEIGHT": ("temporal_decay_weight", float),
}
