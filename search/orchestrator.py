from __future__ import annotations

import os

"""14-phase hybrid search orchestrator for agentic-memory.

Pipeline phases (executed in order):
  Phase 1  — Parse query (normalization, reasoning expansion)
  Phase 2  — Skill-first lookup (conditional early return)
  Phase 3  — Cache check
  Phase 4  — DB setup + filter construction
  Phase 5  — Retrieval (FTS5 BM25 + KG facts)
  Phase 6  — Embedding fallback
  Phase 7  — Hybrid fusion (RRF merge of sparse + dense)
  Phase 8  — Temporal filtering
  Phase 9  — Chunk enhancement + session clustering
  Phase 10 — KG boost + multi-hop traversal
  Phase 11 — Reranking (cross-encoder, late-interaction)
  Phase 12 — Build output items
  Phase 13 — Postprocessing (safety, quality gates, profiling, strong-match, floater)
  Phase 14 — Finalization (record access, telemetry, envelope)

Error handling: each phase is individually isolated. On failure, the
phase increments its error counter (via ``infra.error_counter``) and
the pipeline falls through to the next phase with degraded results.
No single phase failure kills the search.

Thread safety: uses module-level ``_db_columns_cache`` (RLock) and
``_phase_latencies`` (RLock) for cross-call shared state.
"""

import logging
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, Optional

# Shared ThreadPoolExecutor for FTS+KG parallel search (avoids per-query thread creation)
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="search-fts-kg")

# Module-level regex for fact-lookup auto-detection (compiled once, not per-call)
_FACT_LOOKUP_RE = re.compile(
    r"^\s*(what|which)\s+(is|are|was|were)\s+(the\s+)?(current\s+).*(now\s*\?|\?)?\s*$"
    r"|^\s*(what|which)\s+(is|are|was|were)\s+.*\bnow\b"
    r"|^\s*the\s+.*\b(changed|updated|modified)\b.*what\s+is\s+the\s+current"
    r"|^\s*(when|what)\s+(was|is|did)\s+.*\b(last\s+updated|last\s+changed)\b"
    r"|^\s*(has|have)\s+.*\bchanged\b.*\b(since|beginning|start)\b"
    r"|^\s*(what|where|which)\s+.*\b(prefer|stand|latest|figure|getting)\b.*\b(now|moment|current|currently)\b",
    re.IGNORECASE,
)

from infra.cache import (
    _search_cache,
    _search_cache_lock,
    SEARCH_CACHE_MAX,
    SEARCH_CACHE_TTL,
    SEARCH_CACHE_TTL_ENABLED,
    make_cache_key,
)
from infra.memory_common import (
    safe_close_db,
)
from infra.infrastructure import (
    _err,
    ErrorCode,
)
from infra.error_counter import increment as _phase_inc, get_counts as _phase_counts, reset as _phase_reset

# Import functions from other search submodules
from search.query_parser import (
    _parse_search_query,
    _build_zero_result_suggestions,
    _detect_query_type,
    _extract_inference_entity,
)
from search.rerankers import (
    _apply_cross_encoder_rerank,
    _apply_single_ce_rerank,
    _apply_late_interaction_rerank,
    _select_ce_mode,
)
from search.phases.postprocess import (
    apply_safety_demoting as _apply_safety_demoting,
    apply_quality_gates as _apply_quality_gates,
    apply_user_profiling as _apply_user_profiling,
    apply_strong_match_boost as _apply_strong_match_boost,
    apply_save_hint_floater as _apply_save_hint_floater,
)
from search.scoring import (
    _reciprocal_rank_fusion,
    _strong_match_float,
    _compute_final_score,
    _normalize_bm25_ranks,
    compute_channel_weights,
)

# Functions extracted to phase modules
from search.phases._db_utils import (
    _get_memories_columns,
    _fetch_rows_by_ids,  # noqa: F401  (imported by search_pipeline.py from orchestrator)
)
from search.phases.fusion import (
    _merge_chunk_hits,  # noqa: F401  (re-exported via search/__init__.py)
    _search_chunks_enhanced,  # noqa: F401  (imported by search_pipeline.py from orchestrator)
    _hybrid_fusion,
    _enhance_with_chunks,
)
from search.phases.retrieve import (
    _get_embedding_score_threshold,  # noqa: F401  (imported dynamically by eval/test_search_config_unit.py)
    _fts_search,
    _fallback_embedding_search,  # noqa: F401  (re-exported via search/__init__.py + used at runtime)
    _search_kg_facts,
    _reasoning_expand,
)
from search.phases.kg_traversal import (
    _phase_ten_kg_boost,
    _phase_ten_multi_hop_kg,
)
from search.phases.session import _phase_nine_session_cluster, _SESSION_BOOST_FACTOR
from search.phases.envelope import (
    _get_agent_scope,
    _build_result_items,
    _build_empty_result_with_hint,
    _record_last_accessed,
    _build_search_result_envelope,
)
from search.phases.telemetry import (
    _record_search_telemetry,
    _record_search_phase_latencies,
)
from search.drift import (  # noqa: F401
    _record_drift_event,  # imported dynamically by cron/cron_concept_drift.py
    check_concept_drift_db,  # imported dynamically by eval/test_drift_alarms.py
)
from search.feedback import (  # noqa: F401
    record_ctr_feedback_db,  # imported dynamically by mcp_ctr_drift.py / eval
    record_memory_used_in_response,  # imported dynamically by recall/recall.py / eval
)
from search.skill_lookup import _skill_first_lookup

# Docstrings for imported search functions (defined in search.query_parser /
# search.scoring but exposed here as part of the orchestrator's public surface).
_parse_search_query.__doc__ = (
    """Normalize and tokenize a raw search query.

    Args:
        query: Raw natural-language query string from the caller.
        db_path: Path to the SQLite DB (used for synonym expansion).

    Returns:
        A 4-tuple ``(normalized_query, fts_query, bare_text,
        graph_rag_terms)`` where ``normalized_query`` is the
        Unicode-normalized lowercase form, ``fts_query`` is
        FTS5-safe escaped query, ``bare_text`` is the raw extracted
        text, and ``graph_rag_terms`` are tokens for KG expansion.
    """
)
_reciprocal_rank_fusion.__doc__ = (
    """Fuse multiple ranked result lists via Reciprocal Rank Fusion (RRF).

    Args:
        ranked_lists: Iterable of lists, each a ranked sequence of
            doc_ids (or (doc_id, score) tuples) ordered by
            descending relevance.
        k: RRF dampening constant (default 60).
        weights: Optional per-list weight multipliers.  Must be the
            same length as ``ranked_lists``.  ``None`` gives equal
            weight to all lists.

    Returns:
        A ``dict`` mapping ``doc_id`` → float RRF score.  Documents
        appearing in multiple lists receive summed weighted scores.
    """
)
_compute_final_score.__doc__ = (
    """Compute the weighted final score for a single search result.

    Combines six retrieval channels into a single float:
        bm25 (0.40), fitness (0.20), importance (0.15),
        pinned (0.10), recency (0.10), tag_match (0.05).

    Weights are loaded from config ``rerank_weights`` JSON if set,
    otherwise the defaults are used.  Temporal decay / forgetting
    curve is applied by callers AFTER this step.

    Args:
        ctx: A ``ScoreContext`` named-tuple carrying the per-result
            attributes (``rank``, ``fitness``, ``importance``,
            ``pinned``, ``created``, ``tags_json``, ``query``,
            ``boost_pinned``, ``recency_weight``, ``weights``,
            ``now_ts``).

    Returns:
        A float in [0, ~1.5] representing the combined relevance
        score for this result.
    """
)

logger = logging.getLogger(__name__)

_db_columns_cache: dict = {}
_db_columns_cache_lock = threading.Lock()

# Backward-compatible phase latency tracking (pre-error_counter API).
_phase_latencies: dict[str, float] = {}
_phase_latencies_lock = threading.Lock()


@dataclass
class ScoreContext:
    rank: float
    fitness: Optional[float]
    importance: Optional[int]
    pinned: Optional[bool]
    created: Optional[str]
    tags_json: Optional[str]
    query: str
    boost_pinned: bool
    recency_weight: float
    now_ts: Optional[float] = None
    weights: Optional[dict] = None
    is_entailed: Optional[int] = None
    query_tokens: Optional[set] = None  # Pre-computed query tokens for tag matching
    last_accessed: Optional[str] = None
    forget_score: Optional[float] = None  # Neural retention score from memories.score


class MemoryResultRow(NamedTuple):
    """Named columns for a search result row."""

    id: str
    content: str
    source_file: str
    tags: str
    created: str
    rank: float
    final_score: float
    fitness: float
    importance: int
    pinned: int
    last_accessed: Optional[str] = None
    metadata: Optional[str] = None


def _resolve_late_interaction_enabled() -> bool:
    """Eagerly resolve the late-interaction flag from config."""
    try:
        from infra._lazy_imports import get_config

        return bool(getattr(get_config(), "late_interaction", True))
    except (ImportError, AttributeError):
        return True


def _rerank_results(
    *,
    db: Any = None,
    results: list,
    query: str,
    db_path: Path,
    has_fitness: bool,
    rerank: bool,
    boost_pinned: bool,
    recency_weight: float,
    limit: int,
    deep_rerank: bool,
    session_boost_ids: set | None = None,
    as_of: float | None = None,
    budget: "Any | None" = None,
    use_history: bool = True,
) -> tuple[list, Optional[dict]]:
    """Phase 9 of search_memories: compute final scores and rerank.

    Returns ``(results_to_display, ctr_weights)``:

    * ``results_to_display`` is the per-row tuple list (note_id,
      content, source_file, tags_json, created, rank, final_score,
      fitness_score, importance_val, pinned, last_accessed,
      metadata_json, supersedes) — ready for the build-output phase.
    * ``ctr_weights`` is the per-query channel-weight dict used to
      compute final scores, returned for CTR feedback persistence.
      ``None`` when ``rerank=False`` or no fitness column.

    When the table has no ``fitness_score`` column or ``rerank`` is
    disabled, returns the input rows reshaped into the 12-tuple form
    with ``-rank`` as the final_score — a sensible default that keeps
    the result list sorted by FTS rank even without reranking.
    """
    from search.budget_aware import get_search_budget
    if budget is None:
        budget = get_search_budget()
    assert budget is not None
    if not (has_fitness and rerank):
        # No reranking: pass through with -rank as final_score.
        out = []
        for r in results:
            last_accessed_col = r[9] if len(r) > 9 else None
            metadata_json = r[10] if len(r) > 10 else None
            access_count = r[11] if len(r) > 11 else 1
            forget_score = r[12] if len(r) > 12 else None
            supersedes = r[13] if len(r) > 13 else None
            out.append(
                (
                    r[0],
                    r[1],
                    r[2],
                    r[3],
                    r[4],
                    r[5],
                    -r[5],
                    None,
                    None,
                    None,
                    last_accessed_col,
                    metadata_json,
                    supersedes,
                )
            )
        # RANK-FIRST LOCK (PR1.1): the no-rerank pass-through must not
        # mutate the ranking score. Order is fixed by -rank (set above);
        # enrichment is attached as order-invariant envelope fields by
        # _apply_post_rank_metadata in Phase 10.
        return out[:limit], None

    _qtype = _detect_query_type(query)
    # Per-query-type CTR-learned weights override global prior
    from search.scoring import apply_query_type_weights
    _qweights = apply_query_type_weights(_qtype)
    # Global CTR tuning (on by default; MEMORY_CTR_TUNING=0 to disable)
    _ctr_w = compute_channel_weights(db_path)
    if _ctr_w is not None:
        _qweights = _ctr_w
    scored = []
    _pre_query_tokens = None  # Lazy-initialized in scoring loop
    # BM25 normalization: rescale raw FTS5 ranks to [0, 1] before sigmoid
    # so BM25 contributes meaningful discrimination regardless of IDF magnitude.
    _rank_normalized = _normalize_bm25_ranks(results)
    for r in _rank_normalized:
        (
            note_id,
            content,
            source_file,
            tags_json,
            created,
            rank,
            fitness,
            importance,
            pinned,
        ) = r[:9]
        last_accessed = r[9] if len(r) > 9 else None
        metadata_json = r[10] if len(r) > 10 else None
        access_count = r[11] if len(r) > 11 else 1
        forget_score = r[12] if len(r) > 12 else None
        supersedes = r[13] if len(r) > 13 else None
        # Pre-compute query tokens once for tag matching (Phase 8 optimization)
        if _pre_query_tokens is None:
            from search.scoring import _RERANK_TOKEN_RE
            _pre_query_tokens = {
                t.lower() for t in _RERANK_TOKEN_RE.findall(query) if len(t) >= 3
            }
        final_score = _compute_final_score(
            ScoreContext(
                rank=rank,
                fitness=fitness,
                importance=importance,
                pinned=pinned,
                created=created,
                tags_json=tags_json,
                query=query,
                boost_pinned=boost_pinned,
                recency_weight=recency_weight,
                weights=_qweights,
                now_ts=as_of,
                query_tokens=_pre_query_tokens,
                last_accessed=last_accessed,
                forget_score=forget_score,
            )
        )
        # Session-cluster boost — applied to final_score after
        # BM25 normalization so it actually influences ranking (the old
        # pre-normalization rank multiply was erased downstream).
        if session_boost_ids is not None and note_id in session_boost_ids:
            final_score = final_score * _SESSION_BOOST_FACTOR
        importance_val = importance if importance is not None else 3
        fitness_score = fitness if fitness is not None else 0.5
        scored.append(
            (
                note_id,
                content,
                source_file,
                tags_json,
                created,
                rank,
                final_score,
                fitness_score,
                importance_val,
                pinned,
                last_accessed,
                metadata_json,
                supersedes,
            )
        )

    scored = _strong_match_float(scored)
    # PR1.2: CE reranking writes r[6] first (single monotonic CE stage),
    # selected by query type (weak default / chunk for long-multi-part /
    # conversational / deep gated on MEMORY_CE_DEEP). This removes the
    # PR1.1 dual-CE ambiguity where weak+chunk both rewrote r[6] and the
    # last writer owned the order. After CE, ColBERT and answer_rerank
    # may also mutate r[6] (late-interaction and answer-level scoring
    # respectively). The final sort at line ~1840 re-asserts ranking order
    # after all writers. The deep path ("deep") runs the combined baseline
    # then an optional Qwen3-Reranker top-30 refinement that degrades
    # gracefully to combined when the model is unavailable.
    _ce_mode = _select_ce_mode(query, deep_rerank)
    _ce_weak_k = min(len(scored), limit * 2)
    _ce_chunk_k = min(len(scored), limit * 3)
    # Budget check: downgrade to weak CE if tight budget
    if not budget.should_run("chunk_ce", 100):
        _ce_mode = "weak"
        _ce_chunk_k = _ce_weak_k
    out = _apply_single_ce_rerank(
        query, scored, top_k=_ce_chunk_k, mode=_ce_mode,
        weak_k=_ce_weak_k, chunk_k=_ce_chunk_k,
    )
    out = _apply_late_interaction_rerank(query, out, top_k=min(len(out), limit * 2))
    # ColBERT MaxSim reranking (Phase 3): late-interaction via per-token
    # embeddings.  Only fires when index is populated, candidates ≤ 30,
    # query has ≥ 3 tokens, AND budget allows.
    if budget.should_run("colbert", 100):
        try:
            from search.colbert_rerank import colbert_rerank
            out = colbert_rerank(db, query, out, db_path=db_path)
        except Exception as _cb_exc:
            logger.debug("colbert_rerank skipped: %s", _cb_exc)
    # Answer-level reranking (Phase 5): score best snippet per candidate.
    # Uses cross-encoder on extracted snippets, with pre-computed cache.
    if budget.should_run("answer_rerank", 50):
        try:
            from search.answer_rerank import answer_rerank
            from search.enrichment import compute_display_scores

            _display_scores = compute_display_scores(out, query, db_path, as_of=as_of)
            out = answer_rerank(
                db, query, out, db_path=db_path, display_scores=_display_scores
            )
        except Exception as _ar_exc:
            logger.debug("answer_rerank skipped: %s", _ar_exc)
    # LTR reranking: LambdaMART takes over ordering after all CE /
    # late-interaction work.  CE / late-interaction become features
    # into LTR, not final rank owners.  Writes r[6] exactly once.
    if out and budget.should_run("ltr_rerank", 50):
        try:
            from search.ltr.scorer import ltr_rerank, ltr_enabled
            if ltr_enabled():
                from search.ltr.session_ctx import build_session_ctx
                _session_ctx = (
                    build_session_ctx(db, lookback=10, time_window_hours=4.0)
                    if use_history
                    else None
                )
                out = ltr_rerank(
                    query, out, db=db, db_path=db_path,
                    limit=limit, session_ctx=_session_ctx,
                )
        except Exception as _ltr_exc:
            logger.debug("ltr_rerank skipped: %s", _ltr_exc)
    # Temporal SSM recency reranking (gated by temporal_ssm_enabled).  Final
    # recency-aware multiplier on r[6]; neutral (blend 1.0) until
    # cron_train_temporal_ssm has written temporal_ssm_weights, so it cannot
    # destabilize ranking when off or untrained.  Placed after LTR so the
    # trained temporal signal has maximum impact on the final order.
    if out and budget.should_run("temporal_ssm", 20):
        try:
            from search.scoring import _apply_temporal_ssm_rerank
            out = _apply_temporal_ssm_rerank(query, out, as_of=as_of)
        except Exception as _ssm_exc:
            logger.debug("temporal_ssm_rerank skipped: %s", _ssm_exc)
    # RANK-FIRST LOCK (PR1.1): order is owned exclusively by the CE /
    # late-interaction rerankers above (and optionally the LTR stage,
    # which writes r[6] as the final rank owner when a model exists).
    # The four historical enrichment passes (temporal decay,
    # Jaccard surprise, concept boost, centrality boost) must NOT mutate
    # r[6] or re-sort here. They are attached as order-invariant envelope
    # fields by _apply_post_rank_metadata in Phase 10. The gated temporal
    # SSM reranker above is the single sanctioned exception: it is opt-in,
    # neutral until trained, and deliberately the last r[6] writer. Re-assert
    # the ranking order so any future in-place score mutation cannot leak into
    # result ordering.
    out = sorted(
        out,
        key=lambda r: (float(r[6]) if r[6] is not None else 0.0),
        reverse=True,
    )
    return out[:limit], _qweights


def _counting_phase(
    db: AnyConnection,
    results: list,
    query: str,
    limit: int,
) -> list:
    """Counting phase: for 'how many times/how often' queries, count
    distinct values across all matching sessions.

    Returns a synthetic result with the count as the content, so the
    scoring function can match it against the expected number.
    """
    import re as _re

    # Detect counting patterns
    _COUNT_PATTERNS = [
        r"how\s+(many\s+times|often)",
        r"number\s+of\s+times",
        r"count\s+of",
    ]
    is_count = any(_re.search(p, query, _re.IGNORECASE) for p in _COUNT_PATTERNS)
    if not is_count:
        return results

    # Extract topic keywords
    _STOP = {"the", "a", "an", "is", "are", "was", "were", "how", "many",
             "times", "often", "has", "been", "updated", "changed", "modified"}
    keywords = [
        w.lower() for w in _re.findall(r"[a-z]{3,}", query.lower())
        if w.lower() not in _STOP
    ]

    if not keywords:
        return results

    # Search for all sessions mentioning the topic
    for kw in keywords[:3]:
        try:
            rows = db.execute(
                "SELECT m.content FROM memories_fts fts "
                "JOIN tenant_memories m ON m.id = "
                "(SELECT id FROM memories WHERE rowid = fts.rowid) "
                "WHERE memories_fts MATCH ? AND m.deleted_at IS NULL "
                "AND m.category = 'sessions'",
                (f'"{kw}"',)
            ).fetchall()

            if not rows:
                continue

            # Extract values using "X is now Y" pattern
            values = set()
            for (content,) in rows:
                # Match various "is now" patterns
                for pattern in [
                    rf"{_re.escape(kw)}\s+(?:\w+\s+)?is\s+now\s+(\S+(?:\s+\S+)??)(?:\.|;|\n)",
                    rf"{_re.escape(kw)}\s+was\s+(\S+(?:\s+\S+)??)(?:\.|;|\n)",
                    rf"changed\s+to\s+(\S+(?:\s+\S+)??)(?:\.|;|\n)",
                ]:
                    for m in _re.finditer(pattern, content, _re.IGNORECASE):
                        val = m.group(1).strip().rstrip('.')
                        if val and len(val) > 1:
                            values.add(val.lower())

            if values:
                count = len(values)
                # Create a synthetic result with the count
                count_content = f"The {kw} has been updated {count} times"
                synthetic = (
                    f"count_{kw}",
                    count_content,
                    "",  # source_file
                    "[]",  # tags
                    "",  # created_at
                    0,  # rank
                    0.9,  # final_score (high to surface it)
                    None,  # fitness
                    None,  # importance
                    None,  # pinned
                    None,  # last_accessed
                    None,  # metadata
                    None,  # supersedes
                )
                # Add to results if not already present
                existing_ids = {r[0] for r in results}
                if synthetic[0] not in existing_ids:
                    results = list(results) + [synthetic]
                break
        except Exception:
            continue

    results = sorted(results, key=lambda r: float(r[6]) if r[6] is not None else 0.0, reverse=True)
    return results[:limit]


def _temporal_compare(
    db: AnyConnection,
    results: list,
    query: str,
    limit: int,
) -> list:
    """Phase 10.5: Temporal comparison for ordering/counting queries.

    For queries like "Which changed first: X or Y?" or "What was X when Y
    changed?", find the most recent session for each mentioned topic and
    compare timestamps. This is a post-retrieval enrichment that adds
    temporal comparison data to the result set.
    """
    import re as _re

    # Detect temporal comparison patterns
    _TC_PATTERNS = [
        r"which\s+(changed|updated|modified)\s+(first|last|earliest|most recent)",
        r"what\s+was\s+.*\s+when\s+",
        r"before\s+or\s+after",
        r"in\s+what\s+order",
        r"most\s+recently",
    ]
    is_temporal = any(_re.search(p, query, _re.IGNORECASE) for p in _TC_PATTERNS)
    if not is_temporal:
        return results

    # Extract topic keywords from the query (skip stopwords)
    _STOP = {"the", "a", "an", "is", "are", "was", "were", "which", "what",
             "when", "how", "changed", "first", "last", "before", "after",
             "or", "and", "in", "order", "most", "recently", "updated",
             "modified", "was", "the"}
    keywords = [
        w.lower() for w in _re.findall(r"[a-z]{3,}", query.lower())
        if w.lower() not in _STOP
    ]

    if not keywords:
        return results

    # For each keyword, find the most recent session mentioning it
    # and boost that session's final_score
    for kw in keywords[:5]:  # limit to 5 keywords
        try:
            rows = db.execute(
                "SELECT m.id, m.observed_at FROM memories_fts fts "
                "JOIN tenant_memories m ON m.id = "
                "(SELECT id FROM memories WHERE rowid = fts.rowid) "
                "WHERE memories_fts MATCH ? AND m.deleted_at IS NULL "
                "AND m.category = 'sessions' "
                "ORDER BY m.observed_at DESC LIMIT 1",
                (f'"{kw}"',)
            ).fetchall()
            if rows:
                recent_id = rows[0][0]
                # Boost the most recent session for this keyword
                for i, r in enumerate(results):
                    if r[0] == recent_id:
                        old_score = float(r[6]) if r[6] is not None else 0.0
                        results[i] = list(r)
                        from search.config import get_search_config
                        _tc_boost = get_search_config().temporal_compare_boost
                        results[i][6] = old_score * _tc_boost
                        results[i] = tuple(results[i])
                        break
        except Exception:
            continue

    # Re-sort by final_score
    results = sorted(results, key=lambda r: float(r[6]) if r[6] is not None else 0.0, reverse=True)
    return results[:limit]


def _cache_store_result(cache_key: str, result: dict) -> None:
    """Store a search result in the LRU cache and enforce the size cap.

    The 3-line "set + move_to_end + pop oldest" sequence appears in
    every code path that returns a result dict from search_memories.
    Centralizing it here keeps the cache-eviction policy in one place
    — if SEARCH_CACHE_MAX is ever changed (e.g. per-deployment tuning)
    this is the only spot to touch.
    """
    from infra.cache import cache_put, register_cache_note_ids

    note_ids = [
        item.get("id", "")
        for item in (result.get("results") or result.get("result_items") or [])
        if item.get("id")
    ]
    cache_put(cache_key, result, max_size=SEARCH_CACHE_MAX)
    # FIX 1: also mirror the result into the module-level _search_cache dict
    # that the Phase 2 read path inspects, so identical queries register a
    # cache hit. cache_put writes to the same dict, but we mirror it
    # explicitly to keep the inline read path independent of infra.cache
    # internals (TTL/eviction/MAX are applied identically here).
    with _search_cache_lock:
        _search_cache[cache_key] = (time.time(), result)
        _search_cache.move_to_end(cache_key)
        while len(_search_cache) > SEARCH_CACHE_MAX:
            _search_cache.popitem(last=False)
    if note_ids:
        try:
            register_cache_note_ids(cache_key, note_ids)
        except Exception as e:
            logger.warning("register_cache_note_ids failed: %s", e)


def search_memories(
    db_path: Path,
    query: str,
    limit: int = 5,
    include_global: bool = True,
    rerank: bool = True,
    boost_pinned: bool = True,
    recency_weight: float = 0.1,
    include_invalid: bool = True,
    hybrid: bool = True,
    synthesize: bool = False,
    max_synthesis_sentences: int = 5,
    use_history: bool = True,
    safety_wiring: bool = True,
    deep_rerank: bool = False,
    skill_first: bool = False,
    include_facts: bool = True,
    fact_limit: int = 5,
    tenant_id: str = "default",
    light: bool = False,
    as_of: float | None = None,
    belief_status: str | None = None,
    epistemic_source: str | None = None,
    fact_type: str | None = None,
    memory_source: str | None = None,
    category: str = "",
    tags: list[str] | None = None,
    shared_with_me: bool = False,
    mode: str = "hybrid",
) -> dict:
    """Main entry point: 14-phase hybrid search returning ranked memories.

    Pipeline phases (each individually isolated with degrade-on-failure):
      1.  Query parsing + reasoning expansion
      2.  Skill-first lookup (conditional early return)
      3.  Cache check
      4.  DB setup + filter construction (namespace, category, tags)
      5.  Retrieval — FTS5 BM25 + KG facts (parallel)
      6.  Embedding fallback (when FTS returns nothing)
      7.  Hybrid fusion (RRF merge of FTS5 + vector)
      8.  Temporal filtering (valid_to / as_of time-travel)
      9.  Chunk enhancement + session-aware clustering
      10. KG boost + multi-hop traversal
      11. Reranking (cross-encoder, late-interaction, temporal decay, forget curve)
      12. Build output items
      13. Postprocessing (safety demoting, quality gates, user profiling, strong match boost)
      14. Finalization (record access, shared_with_me, audit, envelope, telemetry)

    Orchestrates the full retrieval pipeline: query parsing, FTS5 BM25,
    vector search, ColBERT late-interaction, RRF fusion, cross-encoder
    reranking, temporal decay, neural forget curve, KG concept/centrality
    boost, quality gates, user profiling, and envelope construction.
    Each phase is individually isolated — on failure it increments its
    error counter and the pipeline falls through with degraded results.

    Args:
        db_path: Path to the SQLite memory database.
        query: Natural-language search query.
        limit: Maximum number of results to return.
        include_global: Include memories from all namespaces (not just
            the calling agent's).
        rerank: Enable cross-encoder and late-interaction reranking.
        boost_pinned: Scale pinned notes higher in final ranking.
        recency_weight: Weight for the recency temporal factor.
        include_invalid: Include superseded/invalidated memories.
        hybrid: Enable semantic vector fusion with FTS5.
        synthesize: Generate a synthesis summary alongside results.
        max_synthesis_sentences: Max sentences in synthesis summary.
        use_history: Include session-history context.
        safety_wiring: Run injection-detection safety demoting pass.
        deep_rerank: Use deeper (slower) cross-encoder model.
        skill_first: Return skill matches before memory results.
        include_facts: Include KG facts in the result envelope.
        fact_limit: Max KG facts to return.
        tenant_id: Tenant namespace for multi-tenant isolation.
        light: Skip expensive rerank/personalization passes.
        as_of: Time-travel anchor (epoch seconds) for temporal queries.
        belief_status: Filter KG facts by belief status.
        epistemic_source: Filter KG facts by epistemic source.
        fact_type: Filter KG facts by type.
        memory_source: Filter by memory origin (agent/auto_save/import).
        category: Filter by category slug.
        tags: Filter by tag list (JSON exact-match via LIKE).
        shared_with_me: Append memories shared with the current agent.
        mode: Search mode: "hybrid" (FTS5 + semantic), "semantic" (vector-only),
            "fts" (BM25-only), "facts" (facts-only), "graph" (graph-only).

    Returns:
        A public-API result dict with keys:
          - results: list of result-item dicts
          - count: int (number of results)
          - output: human-readable result string
          - query_id: UUID for CTR feedback correlation
          - agent_scope: current agent namespace
          - related_facts: (optional) KG facts matching the query
          - phase_errors: (optional) per-phase error counters
          - phase_latencies: (optional) per-phase latency in ms
    """
    if not db_path.exists():
        return {
            "results": [],
            "count": 0,
            "output": _err(
                ErrorCode.DB_ERROR,
                f"Memory database not found in current directory ({db_path}). Run memory_rebuild tool first.",
            ),
            "agent_scope": _get_agent_scope(),
            "query_id": uuid.uuid4().hex,
        }

    # Guard: whitespace-only or empty queries should return 0 results
    # immediately, before reasoning-expand can inject spurious OR terms.
    if not query or not query.strip():
        return {
            "results": [],
            "count": 0,
            "output": f"No memories matched the query: '{query}'",
            "suggestions": _build_zero_result_suggestions(db_path, query),
            "agent_scope": _get_agent_scope(),
            "query_id": uuid.uuid4().hex,
        }

    # Reset per-call phase latency accumulator so results are not
    # polluted by stale entries from prior invocations.
    with _phase_latencies_lock:
        _phase_latencies.clear()
    _phase_reset()

    db = None
    try:
        from infra._lazy_imports import connection_pool
        db = connection_pool.get(str(db_path), timeout=30.0, tenant_id=tenant_id)
    except Exception as exc:
        _phase_inc("search.orchestrator", exc)
        logger.warning("search_memories failed to obtain DB connection: %s", exc)
        return {
            "results": [],
            "count": 0,
            "output": _err(ErrorCode.DB_ERROR, f"Search failed to obtain DB connection: {exc}"),
        }

    # Auto-detect fact-lookup queries (module-level regex, compiled once)
    if mode == "hybrid" and _FACT_LOOKUP_RE.search(query):
        mode = "fact_lookup"

    # Phase 1: Parse query
    _t0 = time.time()
    from search.budget_aware import get_search_budget
    # Defer budget creation to after parse — parse_query (semantic expansion)
    # takes ~8s and would exhaust the budget before rerankers run.
    _search_budget = None  # initialized after Phase 1

    if mode == "fact_lookup":
        # Lightweight parse: skip semantic expansion (7s) and graph RAG (1s).
        # FTS5 AND-matching on content words is the right signal for keyword
        # fact lookups. Falls back to OR if AND returns no results.
        from search.query_parser import _normalize_unicode, _STOP_WORDS
        import re as _re
        normalized_query = _normalize_unicode(query)
        _FACT_MODIFIERS = {"current", "now", "latest", "recent"}
        _bare_tokens = [w for w in _re.findall("[\\w@\\#\\.\\+\\-]+", normalized_query, flags=_re.UNICODE)
                        if w.lower() not in _STOP_WORDS and w.lower() not in _FACT_MODIFIERS and len(w) > 1]
        # AND-matching on content words, OR fallback for modifiers
        if _bare_tokens:
            _and_part = " AND ".join(f'"{t}"' for t in _bare_tokens)
            _or_part = " OR ".join(f'"{t}"' for t in _bare_tokens)
            fts_query = f"({_and_part}) OR ({_or_part})"
        else:
            fts_query = ""
        bare_text = " ".join(_bare_tokens)
        graph_rag_terms = []
    else:
        normalized_query, fts_query, bare_text, graph_rag_terms = _parse_search_query(
            query, db_path, conn=db
        )
    _record_phase_latency("parse_query", _t0)
    # A3.2: Reasoning expansion — append entailment-chain objects as OR terms
    # before the cache key is computed so the expanded query is cached.
    # fact_lookup mode skips reasoning expansion — it's 1s of KG traversal
    # that adds noise to keyword-specific queries.
    if mode != "fact_lookup":
        _reasoning_t0 = time.time()
        expansion_terms = _reasoning_expand(db_path, query, conn=db)
        if expansion_terms:
            fts_query = f"{fts_query} OR {' OR '.join(expansion_terms[:5])}"
        _record_phase_latency("reasoning_expand", _reasoning_t0)
    # Drift enforcement for search operations (skipped for fact_lookup —
    # it's non-critical and adds latency to keyword fact queries)
    if mode != "fact_lookup":
        try:
            from infra.config_drift import build_drift_report
            from infra.config_drift_policy import enforce, DriftEnforcementError
            _drift_report = build_drift_report()
            enforce(_drift_report, verb="search")
        except DriftEnforcementError:
            raise
        except Exception:
            logger.debug("drift enforcement skipped in search_memories: non-critical error")
    terms = re.findall("[\\w@\\#\\.\\+\\-]+", fts_query, flags=re.UNICODE)
    if not terms:
        return {
            "results": [],
            "count": 0,
            "output": f"No memories matched the query: '{query}'",
            "suggestions": _build_zero_result_suggestions(db_path, query),
            "agent_scope": _get_agent_scope(),
            "query_id": uuid.uuid4().hex,
        }

    # Initialize budget AFTER parse_query — parse takes ~8s for semantic
    # expansion, and the budget timer starts at creation. Starting it here
    # gives rerankers their full budget allocation.
    _search_budget = get_search_budget()

    # Phase 2: Skill-first lookup (conditional early return)
    if skill_first:
        skill_result = _skill_first_lookup(db_path, terms, limit, tenant_id=tenant_id)
        if skill_result is not None:
            return skill_result

    # Phase 3: Cache check
    cache_key = (
        make_cache_key(
            db_path,
            fts_query,
            limit,
            rerank,
            boost_pinned,
            recency_weight,
            include_invalid,
            include_global,
        )
        + f":mode={mode}"
        + f":cat={category}"
        + f":hybrid={int(hybrid)}"
        + f":light={int(light)}"
        + f":sw={int(safety_wiring)}:dr={int(deep_rerank)}:sf={int(skill_first)}"
        + f":if={int(include_facts)}:fl={int(fact_limit)}"
        + f":as_of={as_of}"
        + f":bs={belief_status or ''}:es={epistemic_source or ''}:ft={fact_type or ''}:ms={memory_source or ''}"
        + (f":tags={','.join(sorted(tags))}" if tags else "")
        + f":swm={int(shared_with_me)}"
        + f":uh={int(use_history)}"
        + f":tid={tenant_id}"
    )
    from infra.cache import cache_touch

    now = time.time()
    # Validate cache entries against DB inode to prevent stale hits on temp DBs.
    # Temp DBs get new inodes on each creation, so a cached result from a
    # previous temp DB would return empty/wrong results.
    try:
        _current_inode = os.stat(str(db_path)).st_ino
    except OSError:
        _current_inode = None
    with _search_cache_lock:
        if cache_key in _search_cache:
            ts, cached_result = _search_cache[cache_key]
            # Check inode if stored (cache entries from before this fix have
            # only 2 elements — treat as valid to avoid mass invalidation)
            _cached_inode = cached_result.get("_inode") if isinstance(cached_result, dict) else None
            if _cached_inode is not None and _current_inode is not None and _cached_inode != _current_inode:
                _search_cache.pop(cache_key)
            elif not SEARCH_CACHE_TTL_ENABLED or now - ts <= SEARCH_CACHE_TTL:
                cache_touch(cache_key)
                cached_result = dict(cached_result)
                cached_result["query_id"] = uuid.uuid4().hex
                if db is not None:
                    try:
                        safe_close_db(db)
                    except Exception:
                        pass
                return cached_result
            else:
                _search_cache.pop(cache_key)

    try:
        _effective_rerank = rerank and not light

        # Phase 4: DB setup + filter construction
        cols = _get_memories_columns(db)
        has_fitness = "fitness_score" in cols
        repo_filter = ""
        # Apply thread-local agent namespace scoping to the SQL search query.
        try:
            from agent_context import get_agent

            ctx = get_agent()
            if ctx.namespace != "default" and ctx.namespace is not None:
                if include_global:
                    repo_filter = f" AND (m.source_file LIKE 'agents/{ctx.namespace}/%' OR m.source_file NOT LIKE 'agents/%')"
                else:
                    repo_filter = f" AND m.source_file LIKE 'agents/{ctx.namespace}/%'"
        except (ImportError, AttributeError):
            pass
        # Sprint 2: memory_source filter (agent / auto_save / import)
        if memory_source is not None:
            source_map = {
                "agent": "m.source_file LIKE 'agents/%' OR m.source_file LIKE 'lessons/%'",
                "auto_save": "m.source_file LIKE 'sessions/auto%'",
                "import": "m.source_file LIKE 'imported/%'",
            }
            clause = source_map.get(memory_source)
            if clause:
                repo_filter = f"{repo_filter} AND ({clause})" if repo_filter else f" AND ({clause})"

        # Category bias — exclude noisy auto-save session
        # transcripts from recall unless the caller explicitly requests a
        # category. The agent can opt back in via category='sessions' or
        # memory_source='auto_save'. The constraint is appended to
        # repo_filter so both FTS and embedding fallback paths inherit it
        # through _fetch_rows_by_ids.
        if category:
            if not re.match(r'^[A-Za-z0-9_-]+$', category):
                category = "lessons"
            repo_filter = f"{repo_filter} AND m.category = ?"
        else:
            # Detect session-related queries and include sessions
            _session_keywords = {"session", "sprint", "incident", "retrospective",
                                 "retro", "debug", "review", "pair", "planning",
                                 "today", "yesterday", "last week", "this week"}
            _q_lower = normalized_query.lower()
            _is_session_query = any(kw in _q_lower for kw in _session_keywords)
            if _is_session_query:
                # Include sessions for session-related queries
                pass  # No filter — sessions are included
            else:
                repo_filter = f"{repo_filter} AND (m.category IS NULL OR m.category != 'sessions')"

        # Sprint 3: tags filter — JSON array exact match via LIKE.
        # Parameterised to prevent SQL injection (was: f-string interpolation
        # of user-supplied tag strings directly into the SQL clause).
        _tag_filter_clauses: list[str] = []
        _tag_filter_params: list[str] = []
        if tags:
            safe_tags = [re.sub(r'[^\w@.#+\-]', '', t) for t in tags]
            safe_tags = [t for t in safe_tags if t]
            for t in safe_tags:
                _tag_filter_clauses.append("m.tags LIKE ?")
                _tag_filter_params.append(f'%"{t}"%')
        _tag_filter_sql = ""
        if _tag_filter_clauses:
            _tag_filter_sql = " AND (" + " AND ".join(_tag_filter_clauses) + ")"

        # Phase 5: Retrieval — FTS5 BM25 + KG facts
        # When search_parallel_enabled is on (default), run FTS and KG fact
        # lookup concurrently — they hit different tables and are independent.
        # When off (or if the feature flag is unavailable), fall back to the
        # original sequential order.
        _t0 = time.time()
        results: list[Any] = []
        related_facts: list[dict] = []
        _search_parallel: bool = False
        try:
            from infra._lazy_imports import get_config
            _search_parallel = bool(getattr(get_config(), "search_parallel_enabled", True))
        except (ImportError, AttributeError):
            _search_parallel = True

        if mode == "facts":
            if include_facts:
                _t0_kg = time.time()
                try:
                    related_facts = _search_kg_facts(
                        db, fts_query, fact_limit, include_invalid,
                        as_of=as_of,
                        belief_status=belief_status,
                        epistemic_source=epistemic_source,
                        fact_type=fact_type,
                    )
                    _record_phase_latency("search.kg_facts", _t0_kg)
                except Exception as _kg_exc:
                    _phase_inc("search.kg_facts", _kg_exc)
                    logger.warning("KG fact retrieval failed: %s", _kg_exc)
            result = _build_search_result_envelope(
                result_items=[],
                output=["Facts mode search completed."],
                results_to_display=[],
                synthesize=synthesize,
                query=query,
                max_synthesis_sentences=max_synthesis_sentences,
                related_facts=related_facts,
            )
            result["compute_budget"] = _search_budget.to_dict()
            return result

        elif mode == "fts":
            results = _fts_search(
                db, fts_query, limit * 5 if _effective_rerank else limit, has_fitness,
                repo_filter,
                tag_filter_sql=_tag_filter_sql,
                tag_filter_params=tuple(_tag_filter_params),
                category=category or None,
            )
            _record_phase_latency("search.fts", _t0)

        elif mode == "fact_lookup":
            # Lightweight path for simple fact-tracking queries ("current value of X").
            # Runs FTS AND-matching with recency ordering through the pipeline's
            # connection pool and filter infrastructure. Skips embedding, hybrid
            # fusion, KG boost, and CE reranking.
            # Recency ordering is essential: "current X" means the most recent
            # session mentioning the topic, not the BM25-best match.
            _fts_limit = limit * 5
            results = _fts_search(
                db, fts_query, _fts_limit, has_fitness,
                repo_filter,
                tag_filter_sql=_tag_filter_sql,
                tag_filter_params=tuple(_tag_filter_params),
                category=category or None,
                recency_order=True,
            )
            _record_phase_latency("search.fts", _t0)
            hybrid = False
            if include_facts:
                _t0_kg = time.time()
                related_facts = _search_kg_facts(
                    db, fts_query, fact_limit, include_invalid,
                    as_of=as_of,
                    belief_status=belief_status,
                    epistemic_source=epistemic_source,
                    fact_type=fact_type,
                )
                _record_phase_latency("search.kg_facts", _t0_kg)
            hybrid = False

        elif mode == "semantic":
            try:
                from search import search_pipeline as _sp_mod
                results = _sp_mod._fallback_embedding_search(
                    db, normalized_query, db_path, limit * 5 if _effective_rerank else limit, repo_filter, category,
                    tag_filter_sql=_tag_filter_sql, tag_filter_params=tuple(_tag_filter_params),
                )
            except ImportError:
                results = []
            _record_phase_latency("search.embedding_fallback", _t0)
            if include_facts:
                _t0_kg = time.time()
                related_facts = _search_kg_facts(
                    db, fts_query, fact_limit, include_invalid,
                    as_of=as_of,
                    belief_status=belief_status,
                    epistemic_source=epistemic_source,
                    fact_type=fact_type,
                )
                _record_phase_latency("search.kg_facts", _t0_kg)
            hybrid = False

        elif mode == "graph":
            results = []
            if include_facts:
                _t0_kg = time.time()
                related_facts = _search_kg_facts(
                    db, fts_query, fact_limit, include_invalid,
                    as_of=as_of,
                    belief_status=belief_status,
                    epistemic_source=epistemic_source,
                    fact_type=fact_type,
                )
                _record_phase_latency("search.kg_facts", _t0_kg)
            hybrid = False

        else:  # mode == "hybrid" (or fallback)
            _prefilter_id_set: set[str] = set()
            try:
                from search.phases.retrieve import _prefilter_ids as _pf  # noqa

                _prefilter_id_set = _pf(db, normalized_query, db_path, limit * 10)
            except Exception as _pf_exc:
                logger.debug("embedding prefilter skipped: %s", _pf_exc)

            if _search_parallel and include_facts:
                def _fts_worker() -> list:
                    conn = connection_pool.get(str(db_path), timeout=10.0, tenant_id=tenant_id)
                    try:
                        return _fts_search(
                            conn, fts_query,
                            limit * 10 if _effective_rerank else limit,
                            has_fitness, repo_filter,
                            tag_filter_sql=_tag_filter_sql,
                            tag_filter_params=tuple(_tag_filter_params),
                            category=category or None,
                            prefilter_ids=_prefilter_id_set or None,
                        )
                    except Exception as _fts_exc:
                        _phase_inc("search.fts", _fts_exc)
                        logger.warning("fts_worker failed: %s", _fts_exc)
                        return []
                    finally:
                        connection_pool.put(conn)

                def _kg_worker() -> list:
                    conn = connection_pool.get(str(db_path), timeout=10.0, tenant_id=tenant_id)
                    try:
                        return _search_kg_facts(
                            conn, fts_query, fact_limit, include_invalid,
                            as_of=as_of,
                            belief_status=belief_status,
                            epistemic_source=epistemic_source,
                            fact_type=fact_type,
                        )
                    except Exception as _kg_exc:
                        _phase_inc("search.kg_facts", _kg_exc)
                        logger.warning("kg_worker failed: %s", _kg_exc)
                        return []
                    finally:
                        connection_pool.put(conn)

                with _EXECUTOR as executor:
                    fts_future = executor.submit(_fts_worker)
                    kg_future = executor.submit(_kg_worker)
                    results = fts_future.result()
                    _record_phase_latency("search.fts", _t0)
                    related_facts = kg_future.result()
                    _record_phase_latency("search.kg_facts", _t0)
            else:
                results = _fts_search(
                    db, fts_query, limit * 5 if _effective_rerank else limit, has_fitness,
                    repo_filter,
                    tag_filter_sql=_tag_filter_sql,
                    tag_filter_params=tuple(_tag_filter_params),
                    category=category or None,
                    prefilter_ids=_prefilter_id_set or None,
                )
                _record_phase_latency("search.fts", _t0)
                if include_facts:
                    _t0_kg = time.time()
                    related_facts = _search_kg_facts(
                        db, fts_query, fact_limit, include_invalid,
                        as_of=as_of,
                        belief_status=belief_status,
                        epistemic_source=epistemic_source,
                        fact_type=fact_type,
                    )
                    _record_phase_latency("search.kg_facts", _t0_kg)

        # Phase 6: Embedding fallback
        if not results:
            _is_opaque = bool(re.fullmatch(r"[A-Za-z0-9_\-]{6,}", query or ""))
            if not _is_opaque and mode == "hybrid":
                try:
                    from search import search_pipeline as _sp_mod2
                    _t0 = time.time()
                    results = _sp_mod2._fallback_embedding_search(
                        db, normalized_query, db_path, limit, repo_filter, category,
                        tag_filter_sql=_tag_filter_sql, tag_filter_params=tuple(_tag_filter_params),
                    )
                except ImportError:
                    results = []
                _record_phase_latency("search.embedding_fallback", _t0)
            if not results:
                try:
                    total = db.execute("SELECT COUNT(*) FROM tenant_memories").fetchone()[0]
                except sqlite3.Error:
                    total = 0
                if total == 0:
                    hint = "The database is empty."
                elif _is_opaque:
                    hint = (
                        "FTS5 returned no exact matches for this opaque token. "
                        "Embedding fallback was skipped (queries that look like "
                        "slugs/IDs have no useful semantic neighbours)."
                    )
                else:
                    hint = f"{mode.upper()} search returned no results."
                return _build_empty_result_with_hint(
                    cache_key=cache_key,
                    query=query,
                    db_path=db_path,
                    hint=hint,
                    related_facts=related_facts if include_facts else None,
                )

        # Phase 7: Hybrid fusion — merge semantic embedding results with FTS5
        # when hybrid is enabled.  When disabled, Phase 6 vector search and
        # Phase 7 RRF merge are skipped entirely.
        _merged_chunks = None
        _fusion_chunk_hits: list = []
        if results and hybrid:
            _t0 = time.time()
            # For inference queries about a specific entity, reduce embedding
            # noise — embeddings match structurally similar sessions that may
            # not contain the entity, drowning out FTS results that do.
            _fusion_emb_override = None
            _inf_entity, _ = _extract_inference_entity(query)
            if _inf_entity:
                from search.config import get_search_config
                _fusion_emb_override = get_search_config().inference_embedding_downweight
            results = _hybrid_fusion(
                db, results, normalized_query, fts_query, db_path, limit, repo_filter, category=category or None,
                chunk_hits_out=_fusion_chunk_hits,
                embedding_weight_override=_fusion_emb_override,
            )
            _record_phase_latency("search.hybrid_fusion", _t0)
            if _fusion_chunk_hits:
                _merged_chunks = _fusion_chunk_hits[0]

        # Phase 8: Temporal filtering
        if not include_invalid or as_of is not None:
            if "valid_to" in cols:
                if as_of is not None:
                    as_of_iso = time.strftime(
                        "%Y-%m-%dT%H:%M:%S", time.gmtime(as_of)
                    )
                    if "valid_from" in cols:
                        valid_ids = {
                            row[0]
                            for row in db.execute(
                                "SELECT id FROM tenant_memories "
                                "WHERE (valid_from IS NULL OR valid_from = '' OR valid_from <= ?) "
                                "AND (valid_to IS NULL OR valid_to = '' OR valid_to > ?)",
                                (as_of_iso, as_of_iso),
                            ).fetchall()
                        }
                    else:
                        valid_ids = {
                            row[0]
                            for row in db.execute(
                                "SELECT id FROM tenant_memories "
                                "WHERE valid_to IS NULL OR valid_to = '' OR valid_to >= ?",
                                (as_of_iso,),
                            ).fetchall()
                        }
                else:
                    valid_ids = {
                        row[0]
                        for row in db.execute(
                            "SELECT id FROM tenant_memories WHERE valid_to IS NULL OR valid_to = ''"
                        ).fetchall()
                    }
                results = [r for r in results if r[0] in valid_ids]
                if not results:
                    return _build_empty_result_with_hint(
                        cache_key=cache_key,
                        query=f"{query} (after temporal filter)",
                        db_path=db_path,
                        hint=None,
                        related_facts=related_facts if include_facts else None,
                    )

        # Phase 9: Chunk enhancement + session clustering
        # fact_lookup mode skips Phases 9-11 (chunk enhancement, KG boost,
        # CE reranking) — these add noise on keyword-specific fact queries
        # and cost ~1.7s per query. FTS5 AND-matching is the right signal.
        if mode != "fact_lookup":
            results = _enhance_with_chunks(
                db, results, fts_query, limit, include_invalid, repo_filter, category=category or None,
                merged_chunks=_merged_chunks,
            )

            # Session-aware clustering
            _t0_sc = time.time()
            session_boost_ids: set = set()
            try:
                results = _phase_nine_session_cluster(
                    results, query, limit, boost_ids=session_boost_ids, db=db
                )
            except Exception as _sc_exc:
                _phase_inc("search.session_cluster", _sc_exc)
                logger.warning("session_cluster failed (degraded): %s", _sc_exc)
            _record_phase_latency("search.session_cluster", _t0_sc)

            # Phase 10: KG boost + multi-hop traversal
            _t0_kgb = time.time()
            try:
                results = _phase_ten_kg_boost(
                    db, results, normalized_query, limit, repo_filter, category=category or None,
                )
            except Exception as _kgb_exc:
                _phase_inc("search.kg_boost", _kgb_exc)
                logger.warning("kg_boost failed (degraded): %s", _kgb_exc)
            _record_phase_latency("search.kg_boost", _t0_kgb)

            # Multi-hop KG traversal
            _t0_mhkg = time.time()
            try:
                results = _phase_ten_multi_hop_kg(
                    db, results, normalized_query, limit, repo_filter, category=category or None,
                )
            except Exception as _mhkg_exc:
                _phase_inc("search.multi_hop_kg", _mhkg_exc)
                logger.warning("multi_hop_kg failed (degraded): %s", _mhkg_exc)
            _record_phase_latency("search.multi_hop_kg", _t0_mhkg)

        # Phase 10.5: Temporal comparison — for queries that ask "which changed
        # first/last" or "what was X when Y changed", find the most recent
        # session for each mentioned topic and compare timestamps.
        if mode != "fact_lookup":
            _t0_tc = time.time()
            try:
                results = _temporal_compare(db, results, query, limit)
            except Exception as _tc_exc:
                _phase_inc("search.temporal_compare", _tc_exc)
                logger.debug("temporal_compare failed (degraded): %s", _tc_exc)
            _record_phase_latency("search.temporal_compare", _t0_tc)

        # Phase 10.6: Counting — for "how many times" queries, count
        # distinct values across all matching sessions.
        if mode != "fact_lookup":
            _t0_cnt = time.time()
            try:
                results = _counting_phase(db, results, query, limit)
            except Exception as _cnt_exc:
                _phase_inc("search.counting", _cnt_exc)
                logger.debug("counting phase failed (degraded): %s", _cnt_exc)
            _record_phase_latency("search.counting", _t0_cnt)

        # Phase 11: Reranking
        # fact_lookup mode skips CE reranking — FTS5 rank is the final rank.
        if mode == "fact_lookup":
            results_to_display = [
                (
                    r[0], r[1], r[2], r[3], r[4], r[5],
                    -r[5], None, None, None,
                    r[9] if len(r) > 9 else None,
                    r[10] if len(r) > 10 else None,
                    r[11] if len(r) > 11 else 1,
                    None,
                )
                for r in results
            ]
            _search_ctr_weights = None
            session_boost_ids = set()
        else:
            _t0 = time.time()
            try:
                results_to_display, _search_ctr_weights = _rerank_results(
                    db=db,
                    results=results,
                    query=query,
                    db_path=db_path,
                    has_fitness=has_fitness,
                    rerank=_effective_rerank,
                    boost_pinned=boost_pinned if not light else False,
                    recency_weight=recency_weight,
                    limit=limit,
                    deep_rerank=deep_rerank,
                    session_boost_ids=session_boost_ids,
                    as_of=as_of,
                    budget=_search_budget,
                    use_history=use_history,
                )
            except Exception as _rerank_exc:
                _phase_inc("search.rerank", _rerank_exc)
                logger.warning(
                    "rerank degraded (falling back to FTS-ranked results): %s", _rerank_exc
                )
                _search_ctr_weights = None
                if has_fitness and _effective_rerank:
                    results_to_display = [
                        (
                            r[0], r[1], r[2], r[3], r[4], r[5],
                            -r[5], None, None, None,
                            r[9] if len(r) > 9 else None,
                            r[10] if len(r) > 10 else None,
                            r[11] if len(r) > 11 else 1,
                            None,
                        )
                        for r in results
                    ]
                else:
                    results_to_display = list(results)
            _record_phase_latency("rerank", _t0)

        # Phase 11.5: Entity-presence boost for inference queries.
        # When the query asks about a specific entity ("Would Caroline have X?"),
        # sessions containing that entity name should be promoted — the CE
        # reranker may demote them if the entity's vocabulary differs from
        # the query's concept keywords.
        # fact_lookup mode skips this — FTS5 rank is already correct.
        if mode != "fact_lookup":
            _entity, _ = _extract_inference_entity(query)
            if _entity and results_to_display:
                _entity_lower = _entity.lower()
                for _rd in results_to_display:
                    _content = (_rd[1] or "").lower() if len(_rd) > 1 else ""
                    if _entity_lower in _content:
                        # Boost final_score (index 6) by entity_boost_factor
                        _old_score = _rd[6] if len(_rd) > 6 else 0
                        if _old_score is not None:
                            from search.config import get_search_config
                            _boost = get_search_config().entity_boost_factor
                            _new_list = list(_rd)
                            _new_list[6] = _old_score * _boost
                            results_to_display[results_to_display.index(_rd)] = tuple(_new_list)
                # Re-sort by final_score descending after boost
                results_to_display.sort(key=lambda x: x[6] if x[6] is not None else 0, reverse=True)

        # Phase 12: Build output items
        result_items, output, backlinks_map = _build_result_items(
            db=db,
            results_to_display=results_to_display,
            query=query,
            rerank=rerank,
            db_path=db_path,
            as_of=as_of,
        )

        # Phase 13: Postprocessing passes — applied in a FIXED, explicit order.
        # Order is contractually significant: safety demoting strips untrusted
        # content first, then quality gates, user profiling, the strong-match
        # boost, and finally the save-hint floater. Every pass is advisory and
        # mutates state.result_items / state.output / state.results_to_display
        # in place; do NOT reorder these without updating the documented order contract.
        # fact_lookup mode skips postprocessing — FTS5 results are already final.
        if not light and mode != "fact_lookup":
            from search.state import PipelineState
            _state = PipelineState(
                db_path=db_path,
                query=query,
                limit=limit,
                rerank=rerank,
                boost_pinned=boost_pinned,
                recency_weight=recency_weight,
                include_invalid=include_invalid,
                hybrid=hybrid,
                deep_rerank=deep_rerank,
                safety_wiring=safety_wiring,
                light=light,
                as_of=as_of,
                tenant_id=tenant_id,
                category=category,
                shared_with_me=shared_with_me,
                db=db,
                normalized_query=normalized_query,
                fts_query=fts_query,
                has_fitness=has_fitness,
                repo_filter=repo_filter,
                effective_rerank=_effective_rerank,
                results=results,
                results_to_display=results_to_display,
                result_items=result_items,
                output=output,
                backlinks_map=backlinks_map,
                related_facts=related_facts,
                session_boost_ids=session_boost_ids,
                ctr_weights=_search_ctr_weights,
            )

            # 13.1 Safety demoting — strip untrusted content before scoring/gates.
            if safety_wiring and _state.result_items:
                _apply_safety_demoting(_state)

            # 13.2 Quality gates
            _apply_quality_gates(_state)

            # 13.3 User profiling
            _apply_user_profiling(_state)

            # 13.4 Strong match boost
            _apply_strong_match_boost(_state)

            # 13.5 Save hint floater
            _apply_save_hint_floater(_state)

            # Read back from state
            result_items = _state.result_items
            output = _state.output
            results_to_display = _state.results_to_display

        # Phase 14: Finalization — record access
        _record_last_accessed(db, result_items)

        # B3.1: shared_with_me post-filter — append shared memories whose
        # source_note_id matches a result and target_agent_id is the
        # current agent, de-duplicating by id.
        if shared_with_me:
            _swm_t0 = time.time()
            try:
                from infra._lazy_imports import get_agent as _swm_get_agent
                _swm_agent_id = _swm_get_agent().agent_id
            except (ImportError, AttributeError):
                _swm_agent_id = None
            if _swm_agent_id:
                _seen_ids = {r[0] for r in results_to_display}
                try:
                    _swm_rows = db.execute(
                        "SELECT source_note_id FROM shared_memories "
                        "WHERE target_agent_id = ? AND source_note_id IS NOT NULL",
                        (_swm_agent_id,),
                    ).fetchall()
                    _swm_source_ids = {r[0] for r in _swm_rows}
                    _new_ids = _swm_source_ids - _seen_ids
                    if _new_ids:
                        _swm_extra = db.execute(
                            f"SELECT id, content, source_file, tags, created_at, "
                            f"importance, category, fitness_score, last_accessed, "
                            f"metadata "
                            f"FROM tenant_memories WHERE id IN ({','.join('?'*len(_new_ids))})",
                            tuple(_new_ids),
                        ).fetchall()
                        if _swm_extra:
                            results_to_display = list(results_to_display) + list(_swm_extra)
                            # FIX 7: also surface shared items in result_items so
                            # the public count/results stay consistent with
                            # raw_results. Build canonical display rows and reuse
                            # the standard result_item builder.
                            _swm_display_rows = []
                            for _r in _swm_extra:
                                try:
                                    (
                                        _sid, _content, _sf, _tags, _created,
                                        _imp, _cat, _fit, _la, _meta,
                                    ) = _r
                                except ValueError:
                                    continue
                                _swm_display_rows.append(
                                    (
                                        _sid, _content, _sf, _tags, _created,
                                        0.0, 0.0, _fit, _imp, 0, _la, _meta,
                                    )
                                )
                            if _swm_display_rows:
                                try:
                                    _swm_items, _, _ = _build_result_items(
                                        db=db,
                                        results_to_display=_swm_display_rows,
                                        query=query,
                                        rerank=rerank,
                                        db_path=db_path,
                                    )
                                    result_items.extend(_swm_items)
                                except Exception as _swm_rie:
                                    logger.warning(
                                        "shared_with_me result_items build failed: %s", _swm_rie
                                    )
                except Exception as _swm_exc:
                    _phase_inc("search.shared_with_me", _swm_exc)
                    logger.warning("shared_with_me filter failed: %s", _swm_exc)
            _record_phase_latency("shared_with_me", _swm_t0)

        # B3.2: Cross-namespace audit logging — fires after search completes
        # when include_global=True and the calling agent is NOT the default namespace.
        _ns_audit_t0 = time.time()
        if include_global:
            try:
                from infra._lazy_imports import get_agent as _ns_audit_agent
                _ns_ctx = _ns_audit_agent()
                if _ns_ctx.namespace not in (None, "default"):
                    try:
                        from infra.audit import enqueue_audit as _ns_enqueue
                        _ns_enqueue(
                            db_path=str(db_path),
                            tool="memory_search",
                            args={
                                "query": query,
                                "include_global": True,
                                "agent_namespace": _ns_ctx.namespace,
                                "shared_with_me": shared_with_me,
                            },
                            results_count=len(result_items),
                            latency_ms=(time.time() - _ns_audit_t0) * 1000.0,
                        )
                    except Exception as _ns_audit_exc:
                        logger.warning("namespace audit enqueue failed: %s", _ns_audit_exc)
            except (ImportError, AttributeError):
                pass
        _record_phase_latency("namespace_audit", _ns_audit_t0)

        result = _build_search_result_envelope(
            result_items=result_items,
            output=output,
            results_to_display=results_to_display,
            synthesize=synthesize,
            query=query,
            max_synthesis_sentences=max_synthesis_sentences,
            related_facts=related_facts if include_facts else None,
        )
        # Add budget status to envelope
        result["compute_budget"] = _search_budget.to_dict()
        # Stamp inode for cache validation (prevents stale hits on temp DBs)
        try:
            result["_inode"] = os.stat(str(db_path)).st_ino
        except OSError:
            pass
        _cache_store_result(cache_key, result)
        _phase_errs = _phase_counts()
        if _phase_errs.get("total_count"):
            result["phase_errors"] = _phase_errs
        if _phase_latencies:
            result["phase_latencies"] = dict(_phase_latencies)
        _qtype = _detect_query_type(query)
        _record_search_telemetry(
            db=db,
            query_id=result["query_id"],
            result_items=result_items,
            ctr_weights=_search_ctr_weights,
            query_type=_qtype,
        )
        _record_search_phase_latencies(
            db=db,
            query_id=result["query_id"],
            phase_latencies=dict(_phase_latencies),
        )
        return result
    except Exception as e:
        _phase_inc("search.orchestrator", e)
        logger.warning("search_memories failed: %s", e)
        return {
            "results": [],
            "count": 0,
            "output": _err(ErrorCode.DB_ERROR, f"Search failed: {e}"),
        }
    finally:
        if db is not None:
            try:
                safe_close_db(db)
            except Exception as e:
                logger.warning("safe_close_db failed: %s", e)


# Backward-compatible phase latency helper for test_observability.py.
def _record_phase_latency(name: str, start_time: float) -> None:
    """Record elapsed wall-clock latency for *name* into _phase_latencies."""
    elapsed_ms = (time.time() - start_time) * 1000.0
    with _phase_latencies_lock:
        _phase_latencies[name] = elapsed_ms
