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

Thread safety: uses module-level ``_db_columns_cache`` (Lock) and
``_phase_latencies`` (Lock) for cross-call shared state.
"""

from __future__ import annotations

import os

import logging
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, Optional, cast

from infra.db import AnyConnection

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

# Regex to detect list, sequence, timeline, or enumeration queries for dynamic candidate pool scaling
_LIST_ENUMERATION_RE = re.compile(
    r"\b(list|sequence|order|steps|phases|items|events|timeline|progressed|chronological|reconstruct|mention\s+only|how\s+did)\b",
    re.IGNORECASE,
)

# Regex to detect complex multi-hop, analytical, or audit queries for automatic deep cross-encoder reranking
_COMPLEX_REASONING_RE = re.compile(
    r"\b(compare|contrast|difference|relationship|why|how\s+does|explain|audit|financial|budget|reconstruct|timeline|multi-step|constraint|analyze|summarize\s+all)\b",
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
    _text_multi_hop_traversal,
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
_phase_latencies_local = threading.local()  # per-call latency dict for concurrent safety


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


def _sanitize_fts_term(term: str) -> str:
    """Wrap an FTS5 term in double-quotes so it is treated as a literal.

    KG expansion terms may contain FTS5 operators (AND, OR, NOT, NEAR),
    wildcards, or other special characters. Wrapping in quotes neutralises
    all of them. Internal double-quotes are escaped by doubling per FTS5.
    """
    return '"' + term.replace('"', '""') + '"'


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
    tenant_id: str = "default",
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
    if not results:
        return [], None

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
    _ce_mode = _select_ce_mode(query, deep_rerank) if rerank else "none"
    _ce_weak_k = min(len(scored), limit * 2)
    _ce_chunk_k = min(len(scored), limit * 3)
    # Budget check: downgrade to weak CE if tight budget
    if rerank and not budget.should_run("chunk_ce", 100):
        _ce_mode = "weak"
        _ce_chunk_k = _ce_weak_k
    out = _apply_single_ce_rerank(
        query, scored, top_k=_ce_chunk_k, mode=_ce_mode,
        weak_k=_ce_weak_k, chunk_k=_ce_chunk_k,
    )
    if rerank:
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

                _display_scores = compute_display_scores(out, query, db_path, as_of=as_of, tenant_id=tenant_id)
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
    from search.scoring import _extract_relative_time_offset_days
    has_rel_time = bool(_extract_relative_time_offset_days(query))
    is_fact_or_temp = bool(_FACT_LOOKUP_RE.search(query)) and not has_rel_time
    def _rank_key(r):
        score = float(r[6]) if r[6] is not None else 0.0
        ts = str(r[4]) if len(r) > 4 and r[4] is not None else ""
        if is_fact_or_temp:
            # Round score to 3 decimal places to group genuinely identical relevance scores, then use timestamp as tie-breaker
            return (round(score, 3), ts)
        return (score, ts)

    if out and len(out) > 1:
        superseded_ids: set[str] = set()
        for item in out:
            sup = item[12] if len(item) > 12 else None
            if sup:
                if isinstance(sup, (list, tuple, set)):
                    superseded_ids.update(str(s) for s in sup)
                else:
                    superseded_ids.add(str(sup))
        if superseded_ids:
            new_out = []
            for item in out:
                item_id = str(item[0])
                if item_id in superseded_ids:
                    item_list = list(item)
                    curr_score = float(item_list[6]) if item_list[6] is not None else 0.0
                    item_list[6] = curr_score * 0.01
                    new_out.append(tuple(item_list))
                else:
                    new_out.append(item)
            out = new_out

    out = sorted(
        out,
        key=_rank_key,
        reverse=True,
    )
    return out[:limit], _qweights


def _counting_phase(
    db: AnyConnection,
    results: list,
    query: str,
    limit: int,
    repo_filter: str = "",
    tenant_id: str | None = None,
) -> list:
    """Counting phase: for 'how many times/how often/how many distinct' queries,
    count distinct values across matching sessions and return both the aggregate
    count and matching provenance notes.
    """
    import re as _re

    _COUNT_PATTERNS = [
        r"how\s+many\s+(?:distinct|different)?\s*values",
        r"how\s+many\s+distinct",
        r"how\s+many\s+(?:updates|changes|transitions|switches)",
        r"how\s+(?:many\s+times|often)\s+.*?\b(?:change|changed|update|updated|switch|switched|transition|modified)\b",
        r"number\s+of\s+times\s+.*?\b(?:change|changed|update|updated|switch|switched|transition|modified)\b",
    ]
    is_count = any(_re.search(p, query, _re.IGNORECASE) for p in _COUNT_PATTERNS)
    if not is_count:
        return results

    # Stopwords specific to counting questions (auxiliary verbs, question frames, pronouns, and action verbs)
    _STOP = {
        "the", "a", "an", "is", "are", "was", "were", "how", "many",
        "times", "often", "has", "been", "updated", "changed", "modified",
        "distinct", "different", "values", "value", "had", "have", "what",
        "does", "did", "do", "doing", "done", "count", "number", "total", "of", "for", "each",
        "since", "beginning", "ever", "in", "history", "all", "switch", "switched",
        "see", "sees", "saw", "seen", "seeing", "meet", "meets", "met", "meeting",
        "play", "plays", "played", "playing", "go", "goes", "went", "gone", "going",
        "get", "gets", "got", "getting", "gotten", "make", "makes", "made", "making",
        "take", "takes", "took", "taken", "taking", "visit", "visits", "visited", "visiting",
        "attend", "attends", "attended", "attending", "talk", "talks", "talked", "talking",
        "work", "works", "worked", "working", "live", "lives", "lived", "living",
        "buy", "buys", "bought", "buying", "spend", "spends", "spent", "spending",
        "with", "and", "or", "to", "my", "your", "his", "her", "their", "our", "me", "you", "them", "us",
        "up", "down", "out", "on", "off", "over", "under", "from", "about", "into", "around", "through", "after", "before", "between", "by", "at",
    }
    keywords = [
        w.lower() for w in _re.findall(r"[a-z0-9_\-\$]{2,}", query.lower())
        if w.lower() not in _STOP
    ]

    if not keywords:
        return results

    # Build search phrases: try full multi-word phrase first, then bigrams, then individual terms
    search_phrases = []
    if len(keywords) >= 2:
        search_phrases.append(" ".join(keywords))
        search_phrases.append(" ".join(keywords[:2]))
    search_phrases.extend(keywords[:3])

    for target in search_phrases:
        # Structured KG State Ledger check first (Option B)
        try:
            from search.temporal_facts import query_state_count_from_ledger
            ledger_state = query_state_count_from_ledger(db, target)
            if ledger_state and ledger_state["count"] > 0:
                count = ledger_state["count"]
                sorted_vals = ", ".join(ledger_state["values"])
                count_content = (
                    f"The {target} has had {count} distinct values in history: {sorted_vals}. "
                    f"Total distinct count: {count}."
                )
                synthetic = (
                    f"count_{target}",
                    count_content,
                    "eval://aggregate",
                    "[]",
                    "",
                    0, 1.0, 1.0, 5, 0, None, "{}", None,
                )
                new_results = [synthetic]
                existing_ids = {synthetic[0]}
                for mem_id in ledger_state.get("source_memories", []):
                    m_row = db.execute(
                        "SELECT id, content, source_file, tags, created_at, observed_at FROM memories WHERE id = ?",
                        (mem_id,)
                    ).fetchone()
                    if m_row and m_row[0] not in existing_ids:
                        existing_ids.add(m_row[0])
                        new_results.append((
                            m_row[0], m_row[1], m_row[2], m_row[3], m_row[4], 0, 0.8,
                            1.0, 3, 0, None, "{}", None,
                        ))
                for r in results:
                    if r[0] not in existing_ids:
                        existing_ids.add(r[0])
                        new_results.append(r)
                return new_results[:limit]
        except Exception:
            pass

        safe_target = target.replace('"', '""')
        try:
            tenant_clause = " AND m.tenant_id = ?" if tenant_id else ""
            params = (f'"{safe_target}"', tenant_id) if tenant_id else (f'"{safe_target}"',)
            # Query all sessions matching the target
            rows = db.execute(
                "SELECT m.id, m.content, m.source_file, m.tags, m.created_at, m.observed_at "
                "FROM memories_fts fts "
                "JOIN memories m ON m.rowid = fts.rowid "
                f"WHERE memories_fts MATCH ? AND m.deleted_at IS NULL{tenant_clause} "
                "AND m.category = 'sessions' "
                "ORDER BY m.observed_at ASC",
                params
            ).fetchall()

            if not rows:
                continue

            values = set()
            val_rows = []
            for row in rows:
                content = row[1] or ""
                summary_line = content.split("\n\n")[0]

                p_multi = rf"\b{_re.escape(target)}\s+(?:is|was|became|changed\s+to|updated\s+to|set\s+to)\s+(?:now\s+)?([^\n.;,]+)"
                p_single = rf"\b([a-z0-9_\-\$]+)\s+{_re.escape(target)}\s+(?:is|was|became|changed\s+to|updated\s+to|set\s+to)\s+(?:now\s+)?([^\n.;,]+)"

                extracted = False
                if " " in target:
                    for m in _re.finditer(p_multi, summary_line, _re.IGNORECASE):
                        val = m.group(1).strip().strip(".'\"")
                        val = _re.split(r'\s+(?:will|blockers|also|good|ready|this|and|but|addition|additional|next|standup|meeting|recap|eod|end)\b', val, flags=_re.IGNORECASE)[0].strip()
                        if val and len(val) > 1:
                            values.add(val.lower())
                            extracted = True
                            break
                else:
                    for m in _re.finditer(p_single, summary_line, _re.IGNORECASE):
                        prefix_word = m.group(1).lower()
                        if prefix_word in {"gift", "travel", "holiday", "birthday", "side", "furniture", "reading"}:
                            continue
                        val = m.group(2).strip().strip(".'\"")
                        val = _re.split(r'\s+(?:will|blockers|also|good|ready|this|and|but|addition|additional|next|standup|meeting|recap|eod|end)\b', val, flags=_re.IGNORECASE)[0].strip()
                        if val and len(val) > 1:
                            values.add(val.lower())
                            extracted = True
                            break
                    if not extracted:
                        for m in _re.finditer(p_multi, summary_line, _re.IGNORECASE):
                            val = m.group(1).strip().strip(".'\"")
                            val = _re.split(r'\s+(?:will|blockers|also|good|ready|this|and|but|addition|additional|next|standup|meeting|recap|eod|end)\b', val, flags=_re.IGNORECASE)[0].strip()
                            if val and len(val) > 1:
                                values.add(val.lower())
                                extracted = True
                                break

                if extracted:
                    val_rows.append(row)

            if values:
                count = len(values)
                sorted_vals = ", ".join(sorted(values))
                count_content = (
                    f"The {target} has had {count} distinct values in history: {sorted_vals}. "
                    f"Total distinct count: {count}."
                )
                synthetic = (
                    f"count_{target}",
                    count_content,
                    "eval://aggregate",
                    "[]",
                    rows[-1][4] if rows else "",
                    0,  # rank
                    1.0,  # final_score (highest priority)
                    1.0,  # fitness
                    5,    # importance
                    0,    # pinned
                    None, # last_accessed
                    "{}", # metadata
                    None, # supersedes
                )
                new_results = [synthetic]
                existing_ids = {synthetic[0]}
                for r in val_rows:
                    if r[0] not in existing_ids:
                        existing_ids.add(r[0])
                        new_results.append((
                            r[0], r[1], r[2], r[3], r[4], 0, 0.8,
                            1.0, 3, 0, None, "{}", None,
                        ))
                for r in results:
                    if r[0] not in existing_ids:
                        existing_ids.add(r[0])
                        new_results.append(r)
                return new_results[:limit]
        except Exception as exc:
            logger.debug("counting_phase target %s error: %s", target, exc)
            continue

    return results


def _extract_query_date_range(query: str) -> tuple[str | None, str | None]:
    """Extract chronological date range (start, end) in ISO-8601 format from natural language query."""
    import re as _re
    from datetime import datetime, timezone

    def _parse_d(d_str: str) -> str | None:
        d_str = d_str.strip().rstrip(",")
        for fmt in ("%Y-%m-%d", "%B %d %Y", "%B %d, %Y", "%b %d %Y", "%b %d, %Y", "%m/%d/%Y", "%d/%m/%Y"):
            try:
                dt = datetime.strptime(d_str, fmt).replace(tzinfo=timezone.utc)
                return dt.isoformat()
            except ValueError:
                pass
        return None

    # 1. ISO format: 2024-08-01 to 2024-10-22
    m1 = _re.search(r"(\d{4}-\d{2}-\d{2})\s+(?:to|until|through|and|-)\s+(\d{4}-\d{2}-\d{2})", query, _re.IGNORECASE)
    if m1:
        d1 = _parse_d(m1.group(1))
        d2 = _parse_d(m1.group(2))
        if d1 and d2:
            return (d1, d2)

    # 2. Textual format: April 8, 2023 to April 25, 2023
    m2 = _re.search(r"(?:from|between)?\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})\s+(?:to|until|through|and|-)\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})", query, _re.IGNORECASE)
    if m2:
        d1 = _parse_d(m2.group(1))
        d2 = _parse_d(m2.group(2))
        if d1 and d2:
            return (d1, d2)

    return (None, None)


def _temporal_compare(
    db: AnyConnection,
    results: list,
    query: str,
    limit: int,
    repo_filter: str = "",
    as_of: float | str | None = None,
    tenant_id: str | None = None,
) -> list:
    """Phase 10.5: Temporal comparison for ordering, recency, and conflicting queries.

    For queries like "Which changed first: X or Y?", "Which was changed most recently: A, B, C?",
    "Has X changed since the beginning? What is it now?", or bounded date-range summaries,
    find matching sessions, compare timestamps, and prioritize the winning/stratified sessions.
    """
    from search.config import get_search_config
    _temporal_compare_boost_cached = get_search_config().temporal_compare_boost
    import re as _re
    from datetime import datetime, timezone, timedelta

    # Relative past date anchor check (e.g. "What did I buy 10 days ago?", "milestone 4 weeks ago")
    if as_of is not None:
        as_of_dt = None
        try:
            if isinstance(as_of, (int, float)):
                as_of_dt = datetime.fromtimestamp(as_of, tz=timezone.utc)
            elif isinstance(as_of, str):
                as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        except Exception:
            pass

        if as_of_dt:
            m_num_days = _re.search(r"(\d+)\s+days?\s+ago", query, _re.I)
            m_num_weeks = _re.search(r"(\d+)\s+weeks?\s+ago", query, _re.I)
            m_num_months = _re.search(r"(\d+)\s+months?\s+ago", query, _re.I)
            target_dt = None
            tolerance = 2

            if m_num_days:
                target_dt = as_of_dt - timedelta(days=int(m_num_days.group(1)))
                tolerance = 2
            elif m_num_weeks:
                target_dt = as_of_dt - timedelta(days=int(m_num_weeks.group(1)) * 7)
                tolerance = 3
            elif m_num_months:
                target_dt = as_of_dt - timedelta(days=int(m_num_months.group(1)) * 30)
                tolerance = 8
            else:
                word_to_num = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "couple of": 2, "couple": 2}
                for word, n in word_to_num.items():
                    if f"{word} months ago" in query.lower() or f"{word} month ago" in query.lower():
                        target_dt = as_of_dt - timedelta(days=n * 30)
                        tolerance = 8
                        break
                    if f"{word} weeks ago" in query.lower() or f"{word} week ago" in query.lower():
                        target_dt = as_of_dt - timedelta(days=n * 7)
                        tolerance = 3
                        break
                    if f"{word} days ago" in query.lower() or f"{word} day ago" in query.lower():
                        target_dt = as_of_dt - timedelta(days=n)
                        tolerance = 2
                        break

            if target_dt:
                d_start = (target_dt - timedelta(days=tolerance)).strftime("%Y-%m-%d")
                d_end = (target_dt + timedelta(days=tolerance)).strftime("%Y-%m-%d")
                q_keywords = [
                    w.lower() for w in _re.findall(r"[a-z0-9_\-\$]{3,}", query.lower())
                    if w.lower() not in {"what", "which", "where", "when", "how", "did", "buy", "bought", "mention", "mentioned", "ago", "days", "weeks", "the", "for"}
                ]
                sql_params: list[Any] = [d_start, d_end]
                tenant_clause = ""
                if tenant_id:
                    tenant_clause = "AND m.tenant_id = ? "
                    sql_params.append(tenant_id)

                try:
                    time_rows = db.execute(
                        f"SELECT m.id, m.content, m.source_file, m.tags, m.created_at, m.observed_at "
                        f"FROM memories m "
                        f"WHERE m.deleted_at IS NULL AND m.category = 'sessions' "
                        f"AND DATE(m.created_at) BETWEEN ? AND ? {tenant_clause}"
                        f"ORDER BY m.created_at DESC LIMIT 10",
                        tuple(sql_params),
                    ).fetchall()
                    if time_rows:
                        new_res = []
                        for tr in time_rows:
                            new_res.append((
                                tr[0], tr[1], tr[2], tr[3], tr[4],
                                0, 1.0, 1.0, 5, 0, None, "{}", None,
                            ))
                        for r in results:
                            if not any(r[0] == nr[0] for nr in new_res):
                                new_res.append(r)
                        return new_res[:limit]
                except Exception:
                    pass

    # Detect temporal comparison / ordering / recency patterns
    _TC_ORDERING_PATTERNS = [
        r"which\s+(?:was\s+)?(changed|updated|modified)\s+(first|last|earliest|most\s+recent|most\s+recently|latest)",
        r"which\s+was\s+changed\s+most\s+recently",
        r"in\s+what\s+order",
        r"before\s+or\s+after",
        r"list\s+(?:the\s+)?order",
        r"walk\s+me\s+through\s+(?:the\s+)?order",
        r"order\s+in\s+which",
        r"in\s+order",
    ]
    _TC_RECENCY_PATTERNS = [
        r"has\s+.*\s+(changed|updated)\s+since",
        r"what\s+is\s+(?:the\s+)?.*\s+now",
        r"what\s+was\s+.*\s+when\s+",
        r"what\s+was\s+.*\s+(before|after)\s+",
        r"most\s+recently",
        r"latest\s+.*",
        r"is\s+the\s+current\s+",
        r"\b(currently|right\s+now|these\s+days|at\s+the\s+moment|presently)\b",
        r"what\s+.*\s+(?:doing|listening|using|going|wearing|reading|eating|working|learning|watching)",
        r"where\s+is\s+(?:the\s+)?next\s+",
        r"when\s+is\s+(?:the\s+)?.*\s+(?:scheduled|happening|taking\s+place)",
        r"when\s+(?:was|is|did)\s+.*\s+(?:last\s+updated|last\s+changed|updated|changed)",
        r"changed\s+several\s+times",
        r"current\s+value",
        r"summarize\s+.*from\s+",
        r"summary\s+of\s+.*from\s+",
        r"progress\s+.*from\s+",
        r"evolved\s+from\s+",
        r"have\s+i\s+.*before",
        r"have\s+i\s+ever\s+",
        r"did\s+i\s+ever\s+",
        r"has\s+.*ever\s+",
    ]

    is_ordering = any(_re.search(p, query, _re.IGNORECASE) for p in _TC_ORDERING_PATTERNS)
    is_recency = any(_re.search(p, query, _re.IGNORECASE) for p in _TC_RECENCY_PATTERNS)

    if not (is_ordering or is_recency):
        return results

    _STOP = {
        "the", "a", "an", "is", "are", "was", "were", "which", "what",
        "when", "how", "changed", "first", "last", "before", "after",
        "or", "and", "in", "order", "most", "recently", "updated",
        "modified", "since", "beginning", "if", "so", "now", "it", "to",
        "has", "been", "earliest", "latest", "doing", "listening", "using",
        "going", "wearing", "reading", "eating", "working", "learning",
        "watching", "currently", "right", "these", "days", "at", "moment",
        "presently", "where", "next", "scheduled", "for", "current", "value",
        "several", "times", "ever", "before", "have", "did", "list", "walk",
        "through", "different", "aspects", "mention", "only", "items",
    }

    # 1. Ordering queries with multiple candidate entities
    if is_ordering:
        candidate_phrases = []
        colon_match = _re.search(r":\s*(?:the\s+)?(.*)", query, _re.IGNORECASE)
        if colon_match:
            raw_candidates = colon_match.group(1).rstrip("?.,")
            parts = _re.split(r",\s*|\s+or\s+|\s+and\s+", raw_candidates)
            candidate_phrases = [
                _re.sub(r"^(?:the|a|an)\s+", "", p.strip(), flags=_re.IGNORECASE).lower()
                for p in parts if p.strip()
            ]

        if not candidate_phrases:
            raw_kw = [
                w.lower() for w in _re.findall(r"[a-z0-9_\-\$]{3,}", query.lower())
                if w.lower() not in _STOP
            ]
            candidate_phrases = raw_kw[:5]

        candidate_records = []
        for cand in candidate_phrases:
            if not cand or cand in _STOP:
                continue
            safe_cand = cand.replace('"', '""')
            try:
                tenant_clause = " AND m.tenant_id = ?" if tenant_id else ""
                params = (f'"{safe_cand}"', tenant_id) if tenant_id else (f'"{safe_cand}"',)
                rows = db.execute(
                    "SELECT m.id, m.content, m.source_file, m.tags, m.created_at, m.observed_at "
                    "FROM memories_fts fts "
                    "JOIN memories m ON m.rowid = fts.rowid "
                    f"WHERE memories_fts MATCH ? AND m.deleted_at IS NULL{tenant_clause} "
                    "AND m.category = 'sessions' "
                    "ORDER BY m.observed_at DESC LIMIT 1",
                    params
                ).fetchall()
                if rows:
                    candidate_records.append((cand, rows[0]))
            except Exception:
                continue

        if candidate_records:
            reverse_sort = not any(w in query.lower() for w in ["first", "earliest"])
            candidate_records.sort(
                key=lambda item: str(item[1][5] or item[1][4] or ""),
                reverse=reverse_sort,
            )
            winner_cand, winner_row = candidate_records[0]

            existing_ids = set()
            new_results = []

            # Winner at Rank 1 with top score
            winner_tuple = (
                winner_row[0], winner_row[1], winner_row[2], winner_row[3],
                winner_row[4], 0, 1.0, 1.0, 5, 0, None, "{}", None,
            )
            new_results.append(winner_tuple)
            existing_ids.add(winner_row[0])

            # Other candidates in chronological order
            for cand, row in candidate_records[1:]:
                if row[0] not in existing_ids:
                    existing_ids.add(row[0])
                    new_results.append((
                        row[0], row[1], row[2], row[3], row[4],
                        0, 0.9, 1.0, 3, 0, None, "{}", None,
                    ))

            # Existing results
            for r in results:
                if r[0] not in existing_ids:
                    existing_ids.add(r[0])
                    new_results.append(r)

            return new_results[:limit]

    # 4. Recency / conflicting fact / contradiction queries
    if is_recency:
        keywords = [
            w.lower() for w in _re.findall(r"[a-z0-9_\-\$]{3,}", query.lower())
            if w.lower() not in _STOP
        ]
        if not keywords:
            return results

        is_contradiction = bool(_re.search(r"\b(contradict|have I ever|did I ever|was changed several times|has .* ever)\b", query, _re.IGNORECASE))

        search_targets = []
        if len(keywords) >= 3:
            search_targets.append(" ".join(keywords[:3]))
        if len(keywords) >= 2:
            search_targets.append(" ".join(keywords[:2]))
        search_targets.extend(keywords[:3])

        for target in search_targets:
            # Structured KG State Ledger check first (Option B)
            try:
                from search.temporal_facts import query_latest_fact_from_ledger
                ledger_fact = query_latest_fact_from_ledger(db, target)
                if ledger_fact and ledger_fact.get("source_memory"):
                    m_row = db.execute(
                        "SELECT id, content, source_file, tags, created_at, observed_at FROM memories WHERE id = ?",
                        (ledger_fact["source_memory"],)
                    ).fetchone()
                    if m_row:
                        new_results = [(
                            m_row[0], m_row[1], m_row[2], m_row[3],
                            m_row[4], 0, 1.0, 1.0, 5, 0, None, "{}", None,
                        )]
                        for r in results:
                            if r[0] != m_row[0]:
                                new_results.append(r)
                        return new_results[:limit]
            except Exception:
                pass

            safe_target = target.replace('"', '""')
            try:
                rows = db.execute(
                    "SELECT m.id, m.content, m.source_file, m.tags, m.created_at, m.observed_at "
                    "FROM memories_fts fts "
                    "JOIN memories m ON m.rowid = fts.rowid "
                    "WHERE memories_fts MATCH ? AND m.deleted_at IS NULL "
                    "AND m.category = 'sessions' "
                    "ORDER BY m.observed_at DESC LIMIT 2",
                    (f'"{safe_target}"',)
                ).fetchall()
                if rows:
                    recent_row = rows[0]
                    new_results = []
                    recent_tuple = (
                        recent_row[0], recent_row[1], recent_row[2], recent_row[3],
                        recent_row[4], 0, 1.0, 1.0, 5, 0, None, "{}", None,
                    )
                    new_results.append(recent_tuple)

                    # For contradiction queries, also pair the prior historical assertion
                    if is_contradiction and len(rows) > 1:
                        prior_row = rows[-1]
                        if prior_row[0] != recent_row[0]:
                            new_results.append((
                                prior_row[0], prior_row[1], prior_row[2], prior_row[3],
                                prior_row[4], 0, 0.95, 1.0, 4, 0, None, "{}", None,
                            ))

                    for r in results:
                        if not any(r[0] == nr[0] for nr in new_results):
                            new_results.append(r)
                    return new_results[:limit]
            except Exception:
                continue

    return results


def _conjoint_entity_phase(
    db: AnyConnection,
    results: list,
    query: str,
    limit: int,
    tenant_id: str | None = None,
) -> list:
    """Phase 11.4: Conjoint Entity Retrieval for compound multi-entity queries.

    When a query asks for multiple distinct items (e.g. "What are the current X and Y?"
    or "combining X and Y" or "cost of the X and Y"), a single blended search can let the
    entity with higher term frequency dominate all top candidate slots. This phase extracts
    the conjuncts, retrieves the most recent / best matching session for each entity
    independently, and ensures all entities are interleaved into the top positions.
    """
    import re as _re

    conjuncts = []

    # Pattern 0: Quoted entity titles: 'X' and 'Y' or "X" and "Y"
    quoted_matches = _re.findall(r"['\"]([^'\"]+)['\"]", query)
    if len(quoted_matches) >= 2:
        conjuncts = [quoted_matches[0].strip(), quoted_matches[1].strip()]

    # Pattern 1: "What are the current X and Y?", "What is the X and Y?"
    if not conjuncts:
        m1 = _re.search(
            r"what\s+(?:is|are|was|were)\s+(?:the\s+)?(?:current\s+)?(.+?)\s+and\s+(?:the\s+)?(.+?)\s*\??$",
            query,
            _re.IGNORECASE,
        )
        if m1:
            raw_e1, raw_e2 = m1.group(1).strip(), m1.group(2).strip()
            _STOP = {"the", "a", "an", "current", "latest", "now", "is", "are", "were", "was"}
            e1_words = [w for w in raw_e1.split() if w.lower() not in _STOP]
            e2_words = [w for w in raw_e2.split() if w.lower() not in _STOP]
            if e1_words and e2_words:
                conjuncts = [" ".join(e1_words), " ".join(e2_words)]

    # Pattern 2: "combining my X and Y projects/data/work"
    if not conjuncts:
        m2 = _re.search(
            r"combining\s+(?:my\s+)?([a-zA-Z0-9_\-\$ ]+?)\s+and\s+([a-zA-Z0-9_\-\$ ]+?)\s+(?:projects|tasks|work|efforts|data|systems)",
            query,
            _re.IGNORECASE,
        )
        if m2:
            conjuncts = [m2.group(1).strip(), m2.group(2).strip()]

    # Pattern 3: "across the X, Y, and Z"
    if not conjuncts:
        m3 = _re.search(
            r"across\s+(?:the\s+)?([a-zA-Z0-9_\-\$ ]+?),\s+([a-zA-Z0-9_\-\$ ]+?),\s+and\s+([a-zA-Z0-9_\-\$ ]+)",
            query,
            _re.IGNORECASE,
        )
        if m3:
            conjuncts = [m3.group(1).strip(), m3.group(2).strip(), m3.group(3).strip()]

    # Pattern 4: Generic compound noun/item conjunctions (cost/spent/total/views of X and Y)
    if not conjuncts:
        m4 = _re.search(
            r"(?:total|sum|combined|cost|spend|spent|distance|views|comments|episodes|number\s+of\s+[\w\s]+|minimum\s+amount|between|difference|more\s+than|compared\s+to|sold\s+the)\s+(?:of|for|on|from|in|between)?\s+(?:the\s+|my\s+)?([A-Za-z0-9_\-\$ ]{2,35}?)\s+and\s+(?:the\s+|my\s+)?([A-Za-z0-9_\-\$ ]{2,35}?)(?:\s+(?:i\s+(?:purchased|bought|attended|visited|listened|watched|spent|did)|combined|in\s+total|\?|$))",
            query,
            _re.IGNORECASE,
        )
        if m4:
            raw_c1, raw_c2 = m4.group(1).strip(), m4.group(2).strip()
            _STOP_C = {"the", "a", "an", "my", "our", "all", "what", "how", "is", "of", "total"}
            c1_words = [w for w in raw_c1.split() if w.lower() not in _STOP_C]
            c2_words = [w for w in raw_c2.split() if w.lower() not in _STOP_C]
            if c1_words and c2_words:
                conjuncts = [" ".join(c1_words), " ".join(c2_words)]

    # Pattern 5: Generalized "A and B" conjunction split
    if not conjuncts and " and " in query.lower():
        cleaned_q = query.rstrip("?. ").strip()
        parts = _re.split(r"\s+and\s+", cleaned_q, flags=_re.IGNORECASE)
        if len(parts) == 2:
            p1, p2 = parts[0].strip(), parts[1].strip()
            _LEADING_STOP = r"^(?:what|how|which|can|could|is|are|was|were|total|number|amount|cost|weight|time|minimum|average|distance|page\s+count|difference|more|less|gpa|did|do|have|i|spend|spent|get|got|if|sold|bought|purchased|finish|finished|take|takes|in|on|of|from|between|for|the|my|a|an|two|three|all|items?|meals?|novels?|comments?|feed|trip|trips?|events?|days?|weeks?|months?|years?|lunch\s+meals?\s+i\s+got\s+from|comments\s+on\s+my\s+recent|novels\s+i\s+finished\s+in|days\s+i\s+spent\s+in|it\s+takes\s+i\s+to|money\s+i\s+spent\s+on)\s+"
            p1_clean = p1
            for _ in range(5):
                next_p1 = _re.sub(_LEADING_STOP, "", p1_clean, flags=_re.IGNORECASE).strip()
                if next_p1 == p1_clean:
                    break
                p1_clean = next_p1

            p2_clean = _re.sub(r"\s+(?:i\s+(?:purchased|bought|attended|visited|listened|watched|spent|got|finished|inherited|read)|combined|in\s+total|studies|last\s+year|this\s+year|together)$", "", p2, flags=_re.IGNORECASE).strip()
            p2_clean = _re.sub(r"^(?:the\s+|my\s+|a\s+|an\s+)", "", p2_clean, flags=_re.IGNORECASE).strip()

            if len(p1_clean) >= 2 and len(p2_clean) >= 2:
                conjuncts = [p1_clean, p2_clean]

    # Pattern 6: "X than Y" comparative queries
    if not conjuncts:
        m_than = _re.search(
            r"\b(?:more|less|greater|higher)\s+(?:was|did|is|have)?\s+(?:the\s+)?([A-Za-z0-9_\-\$ ]+?)\s+than\s+(?:the\s+)?([A-Za-z0-9_\-\$ ]+?)(?:\?|$)",
            query,
            _re.IGNORECASE
        )
        if m_than:
            conjuncts = [m_than.group(1).strip(), m_than.group(2).strip()]

    if not conjuncts:
        return results

    _STOP_WORDS = {"the", "a", "an", "my", "our", "all", "in", "to", "for", "of", "and", "from", "on", "with", "by", "at", "is", "was", "were", "are", "i", "did", "do", "what", "how", "total", "amount", "number"}
    conjoint_rows = []
    for conj in conjuncts:
        clean_conj = " ".join(w for w in conj.split() if w.lower() not in _STOP_WORDS)
        if not clean_conj:
            continue
        safe_conj = clean_conj.replace('"', '""')
        try:
            where_sql = "memories_fts MATCH ? AND m.deleted_at IS NULL"
            params: list[Any] = [f'"{safe_conj}"']
            if tenant_id:
                where_sql += " AND m.tenant_id = ?"
                params.append(tenant_id)

            rows = db.execute(
                f"SELECT m.id, m.content, m.source_file, m.tags, m.created_at, m.observed_at "
                f"FROM memories_fts fts "
                f"JOIN memories m ON m.rowid = fts.rowid "
                f"WHERE {where_sql} "
                f"ORDER BY fts.rank LIMIT 2",
                tuple(params)
            ).fetchall()
            if rows:
                conjoint_rows.extend(rows)
            else:
                kw_terms = [f'"{w}"' for w in clean_conj.split() if w.lower() not in _STOP_WORDS and len(w) >= 3]
                if kw_terms:
                    kw_where = "memories_fts MATCH ? AND m.deleted_at IS NULL"
                    kw_params: list[Any] = [" AND ".join(kw_terms)]
                    if tenant_id:
                        kw_where += " AND m.tenant_id = ?"
                        kw_params.append(tenant_id)
                    rows_kw = db.execute(
                        f"SELECT m.id, m.content, m.source_file, m.tags, m.created_at, m.observed_at "
                        f"FROM memories_fts fts "
                        f"JOIN memories m ON m.rowid = fts.rowid "
                        f"WHERE {kw_where} "
                        f"ORDER BY fts.rank LIMIT 2",
                        tuple(kw_params)
                    ).fetchall()
                    if not rows_kw:
                        kw_params_or: list[Any] = [" OR ".join(kw_terms)]
                        if tenant_id:
                            kw_params_or.append(tenant_id)
                        rows_kw = db.execute(
                            f"SELECT m.id, m.content, m.source_file, m.tags, m.created_at, m.observed_at "
                            f"FROM memories_fts fts "
                            f"JOIN memories m ON m.rowid = fts.rowid "
                            f"WHERE {kw_where} "
                            f"ORDER BY fts.rank LIMIT 2",
                            tuple(kw_params_or)
                        ).fetchall()
                    if rows_kw:
                        conjoint_rows.extend(rows_kw)
        except Exception:
            continue

    if conjoint_rows:
        existing_ids = set()
        new_results = []
        for idx, row in enumerate(conjoint_rows):
            if row[0] not in existing_ids:
                existing_ids.add(row[0])
                new_results.append((
                    row[0], row[1], row[2], row[3], row[4],
                    0, 1.0 - (idx * 0.05), 1.0, 5, 0, None, "{}", None,
                ))
        for r in results:
            if r[0] not in existing_ids:
                existing_ids.add(r[0])
                new_results.append(r)
        return new_results[:limit]

    return results


def clear_orchestrator_caches() -> None:
    """Clear orchestrator db columns cache."""
    with _db_columns_cache_lock:
        _db_columns_cache.clear()


def _cache_store_result(cache_key: str, result: dict, db_path: Path | str | None = None) -> None:
    """Store a search result in the LRU cache and enforce the size cap.

    The 3-line "set + move_to_end + pop oldest" sequence appears in
    every code path that returns a result dict from search_memories.
    Centralizing it here keeps the cache-eviction policy in one place
    — if SEARCH_CACHE_MAX is ever changed (e.g. per-deployment tuning)
    this is the only spot to touch.
    """
    from infra.cache import cache_put, register_cache_note_ids

    if isinstance(result, dict) and "_inode" not in result:
        p_str = str(db_path) if db_path is not None else cache_key.split(":")[0]
        if p_str and p_str != ":memory:":
            try:
                if os.path.exists(p_str):
                    result["_inode"] = os.stat(p_str).st_ino
            except OSError:
                pass

    note_ids = [
        item.get("id", "")
        for item in (result.get("results") or result.get("result_items") or [])
        if item.get("id")
    ]
    cache_put(cache_key, result, max_size=SEARCH_CACHE_MAX)
    if note_ids:
        try:
            register_cache_note_ids(cache_key, note_ids)
        except Exception as e:
            logger.warning("register_cache_note_ids failed: %s", e)



def _check_crdt_staleness(db: AnyConnection, result_items: list) -> None:
    """Best-effort check: log when memories.content is stale vs memory_field_crdt.

    Samples each result item's note_id and compares the ``content`` column
    in ``memories`` to the winning CRDT field value.  A mismatch means a
    remote CRDT merge updated ``memory_field_crdt`` but the SQL row was
    not yet projected back.  This is log-only — no mutations are made.
    """
    if not result_items:
        return
    try:
        note_ids = [item.get("id", "") for item in result_items if item.get("id")]
        if not note_ids:
            return
        placeholders = ",".join("?" for _ in note_ids)
        # Check if memory_field_crdt table exists
        tables = {
            r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_field_crdt'"
            ).fetchall()
        }
        if "memory_field_crdt" not in tables:
            return
        rows = db.execute(
            f"SELECT m.id, m.content, c.value "
            f"FROM memories m "
            f"JOIN memory_field_crdt c ON c.memory_id = m.id AND c.field_name = 'content' "
            f"WHERE m.id IN ({placeholders}) AND c.is_deleted = 0",
            note_ids,
        ).fetchall()
        stale_count = sum(1 for r in rows if (r[1] or "") != (r[2] or ""))
        if stale_count:
            logger.warning(
                "CRDT staleness: %d/%d search results have stale memories.content "
                "vs memory_field_crdt.  Run project_crdt_to_sql to repair.",
                stale_count,
                len(rows),
            )
    except Exception as exc:
        logger.debug("_check_crdt_staleness failed: %s", exc)


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
    db_path = Path(db_path)
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

    # M8 fix: per-call latency dict via threading.local() so concurrent
    # searches don't overwrite each other's phase timings.
    _phase_latencies_local.latencies = {}
    with _phase_latencies_lock:
        _phase_latencies.clear()
    _phase_reset()

    db = None
    try:
        from infra._lazy_imports import connection_pool
        db = connection_pool.get(str(db_path), timeout=30.0, tenant_id=tenant_id)
        # Set include_global flag so tenant_memories view returns all tenants
        # when the caller requests cross-tenant search.
        from infra.db import set_include_global
        set_include_global(include_global)
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

    # Dynamic candidate retrieval expansion for sequence/list queries
    if _LIST_ENUMERATION_RE.search(query):
        limit = max(limit, 30)

    # Automatic Adaptive Tiered Retrieval: escalate to deep cross-encoder for complex reasoning/audit queries
    if mode == "hybrid" and not light and _COMPLEX_REASONING_RE.search(query):
        deep_rerank = True
        rerank = True

    # Phase 1: Parse query
    _t0 = time.time()
    from search.budget_aware import get_search_budget
    # Defer budget creation to after parse — parse_query (semantic expansion)
    # takes ~8s and would exhaust the budget before rerankers run.
    _search_budget = None  # initialized after Phase 1

    if mode in ("fact_lookup", "fts"):
        # Lightweight parse: skip semantic expansion (~10s), graph RAG (~1s),
        # reasoning expansion (~1s), and drift enforcement.
        from search.query_parser import normalize_unicode, _STOP_WORDS, _QUERY_EXPANSIONS
        import re as _re
        normalized_query = normalize_unicode(query)
        _fact_stop = _STOP_WORDS | {
            "current", "now", "what", "is", "the", "latest", "present",
            "presently", "currently", "changed", "since", "beginning", "if", "so",
            "last", "updated", "modified", "and", "or", "stand", "stands", "prefer",
            "prefers", "get", "gets", "going", "trip", "scheduled", "used", "days",
            "at", "in", "for", "to", "of", "with", "by", "from", "on", "about",
            "tell", "me", "there", "where", "does", "moment", "are", "were", "was",
            "how", "when", "which", "who", "whom", "this", "that", "these", "those",
        }
        _bare_tokens = [w for w in _re.findall("[\\w@\\#\\.\\+\\-]+", normalized_query, flags=_re.UNICODE)
                        if w.lower() not in _fact_stop and len(w) > 1]

        # Check query expansions (synonyms like framework -> tech stack, exercise -> workout plan)
        _expanded_terms = []
        q_lower = query.lower()
        for trigger, exps in _QUERY_EXPANSIONS.items():
            if _re.search(rf"\b{_re.escape(trigger)}\b", q_lower):
                _expanded_terms.extend(exps)

        if _bare_tokens or _expanded_terms:
            _bigrams = []
            if len(_bare_tokens) >= 2:
                for _idx in range(len(_bare_tokens) - 1):
                    _bigrams.append(f"{_bare_tokens[_idx]} {_bare_tokens[_idx+1]}")

            phrase_parts = [f'"{b}"' for b in _bigrams] + [f'"{e}"' for e in _expanded_terms]
            token_parts = [f'"{t}"' for t in _bare_tokens if len(t) > 1]
            if phrase_parts:
                fts_query = " OR ".join(phrase_parts + token_parts)
            else:
                fts_query = " OR ".join(token_parts)
        else:
            fts_query = ""
        bare_text = " ".join(_bare_tokens)
        graph_rag_terms: list[str] = []
    else:
        mode_effective = "light" if light else mode
        normalized_query, fts_query, bare_text, graph_rag_terms = _parse_search_query(
            query, db_path, conn=db, mode=mode_effective
        )
        _reasoning_t0 = time.time()
        expansion_terms = _reasoning_expand(db_path, query, conn=db)
        if expansion_terms:
            fts_query = f"{fts_query} OR {' OR '.join(_sanitize_fts_term(t) for t in expansion_terms[:5])}"
        _record_phase_latency("reasoning_expand", _reasoning_t0)
        try:
            from infra.config_drift import build_drift_report
            from infra.config_drift_policy import enforce, DriftEnforcementError
            _drift_report = build_drift_report()
            enforce(_drift_report, verb="search")
        except DriftEnforcementError:
            raise
        except Exception:
            logger.debug("drift enforcement skipped in search_memories: non-critical error")
    _record_phase_latency("parse_query", _t0)
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
        _raw_terms = re.findall(r"\b[a-zA-Z0-9_\-]{3,}\b", query.lower())
        skill_result = _skill_first_lookup(db_path, _raw_terms, limit, tenant_id=tenant_id)
        if skill_result is not None:
            return skill_result

    # Phase 3: Cache check
    # Resolve agent context BEFORE the cache key so namespace scoping is
    # included — without this, two agents with different namespaces but
    # the same tenant would share cached results (P0-6 cache poisoning).
    _ns = ""
    _agent_id = ""
    try:
        from agent_context import get_agent as _get_agent_for_cache

        _ctx_cache = _get_agent_for_cache()
        if _ctx_cache.namespace and _ctx_cache.namespace != "default":
            _ns = _ctx_cache.namespace
        if _ctx_cache.agent_id:
            _agent_id = _ctx_cache.agent_id
    except (ImportError, AttributeError):
        pass
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
        + f":ns={_ns}:aid={_agent_id}"
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
                    except Exception as exc:
                        logger.debug("safe_close_db failed on cache hit path (non-fatal): %s", exc)
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
                # H15: validate namespace and escape LIKE metacharacters
                _ns = ctx.namespace
                if not re.fullmatch(r"[A-Za-z0-9._-]+", _ns):
                    raise ValueError(f"Invalid agent namespace: {_ns!r}")
                _safe_ns = _ns.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                if include_global:
                    repo_filter = f" AND (m.source_file LIKE 'agents/{_safe_ns}/%' ESCAPE '\\' OR m.source_file NOT LIKE 'agents/%')"
                else:
                    repo_filter = f" AND m.source_file LIKE 'agents/{_safe_ns}/%' ESCAPE '\\'"
        except (ImportError, AttributeError, ValueError) as _ns_exc:
            logger.debug("Namespace filtering disabled: %s", _ns_exc)
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

        # category filter — appended to repo_filter so FTS and embedding
        # paths both inherit it. The value must go into _tag_filter_params
        if category and category.lower() == "all":
            # Explicitly search across all categories including sessions
            pass
        elif category:
            if not re.match(r'^[A-Za-z0-9_-]+$', category):
                category = "lessons"
            repo_filter = f"{repo_filter} AND m.category = ?"
            _tag_filter_params.append(category)
        else:
            # Detect episodic/conversational queries and tenant-isolated searches and include sessions
            _session_keywords = {
                "session", "sprint", "incident", "retrospective",
                "retro", "debug", "review", "pair", "planning",
                "today", "yesterday", "last week", "this week",
                " i ", " my ", " me ", " we ", " our ", "did i",
                "have i", "how many", "how much", "how long",
                "what did", "where did", "when did", "who did",
                "which", "what is", "what was", "what were", "where is",
                "when is", "who is", "who was", "order of", "before", "after"
            }
            _q_padded = f" {normalized_query.lower()} "
            _is_session_query = bool(tenant_id) or any(kw in _q_padded for kw in _session_keywords)
            if _is_session_query:
                pass  # No filter — sessions are included
            else:
                repo_filter = f"{repo_filter} AND (m.category IS NULL OR m.category != 'sessions')"

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
                tenant_id=tenant_id,
            )
            _record_phase_latency("search.fts", _t0)
            hybrid = False

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
                tenant_id=tenant_id,
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
            results = _fallback_embedding_search(
                db, normalized_query, db_path, limit * 5 if _effective_rerank else limit, repo_filter, category,
                tag_filter_sql=_tag_filter_sql, tag_filter_params=tuple(_tag_filter_params),
            )
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
            if not hybrid:
                mode = "fts"
            # P0 fix #7: prefilter is disabled — ANN-shortlisted IDs are
            # almost all auto-save sessions that don't match the keyword
            # FTS query, causing empty results every time. The prefilter
            # also loads the embedding model twice (once in _semantic_expand,
            # once here) triggering loky semaphore leaks on macOS. FTS
            # without prefilter works correctly; hybrid fusion blends
            # FTS + embedding results in Phase 7.

            if _search_parallel and include_facts:
                def _fts_worker() -> list:
                    from infra.db import set_include_global
                    set_include_global(include_global)
                    conn = connection_pool.get(str(db_path), timeout=10.0, tenant_id=tenant_id)
                    try:
                        return _fts_search(
                            conn, fts_query,
                            limit * 10 if _effective_rerank else max(limit * 5, 50),
                            has_fitness, repo_filter,
                            tag_filter_sql=_tag_filter_sql,
                            tag_filter_params=tuple(_tag_filter_params),
                            category=category or None,
                            prefilter_ids=None,
                            tenant_id=tenant_id,
                            include_global=include_global,
                        )
                    except Exception as _fts_exc:
                        _phase_inc("search.fts", _fts_exc)
                        logger.warning("fts_worker failed: %s", _fts_exc)
                        return []
                    finally:
                        connection_pool.put(conn)

                def _kg_worker() -> list:
                    from infra.db import set_include_global
                    set_include_global(include_global)
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

                fts_future = _EXECUTOR.submit(_fts_worker)
                kg_future = _EXECUTOR.submit(_kg_worker)
                results = fts_future.result()
                _record_phase_latency("search.fts", _t0)
                related_facts = kg_future.result()
                _record_phase_latency("search.kg_facts", _t0)
            else:
                results = _fts_search(
                    db, fts_query, limit * 5 if _effective_rerank else max(limit * 5, 50), has_fitness,
                    repo_filter,
                    tag_filter_sql=_tag_filter_sql,
                    tag_filter_params=tuple(_tag_filter_params),
                    category=category or None,
                    prefilter_ids=None,
                    tenant_id=tenant_id,
                    include_global=include_global,
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
                    results = _sp_mod2._fallback_embedding_search(  # type: ignore[attr-defined]
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
                tenant_id=tenant_id,
            )
            _record_phase_latency("search.hybrid_fusion", _t0)
            if _fusion_chunk_hits:
                _merged_chunks = _fusion_chunk_hits[0]

        # Phase 8: Temporal filtering
        if results and (not include_invalid or as_of is not None):
            if "valid_to" in cols:
                _cand_ids = [r[0] for r in results]
                if _cand_ids:
                    _ph = ",".join("?" for _ in _cand_ids)
                    if as_of is not None:
                        if isinstance(as_of, str):
                            as_of_iso = as_of[:19].replace("Z", "")
                        elif isinstance(as_of, datetime):
                            as_of_iso = as_of.strftime("%Y-%m-%dT%H:%M:%S")
                        else:
                            try:
                                as_of_iso = time.strftime(
                                    "%Y-%m-%dT%H:%M:%S", time.gmtime(float(as_of))
                                )
                            except Exception:
                                as_of_iso = str(as_of)[:19]

                        if "valid_from" in cols:
                            valid_rows = db.execute(
                                f"SELECT id FROM memories WHERE id IN ({_ph}) AND deleted_at IS NULL "
                                "AND (valid_from IS NULL OR valid_from = '' OR valid_from <= ?) "
                                "AND (valid_to IS NULL OR valid_to = '' OR valid_to > ?)",
                                tuple(_cand_ids) + (as_of_iso, as_of_iso),
                            ).fetchall()
                        else:
                            valid_rows = db.execute(
                                f"SELECT id FROM memories WHERE id IN ({_ph}) AND deleted_at IS NULL "
                                "AND (valid_to IS NULL OR valid_to = '' OR valid_to > ?)",
                                tuple(_cand_ids) + (as_of_iso,),
                            ).fetchall()
                    else:
                        valid_rows = db.execute(
                            f"SELECT id FROM memories WHERE id IN ({_ph}) AND deleted_at IS NULL "
                            "AND (valid_to IS NULL OR valid_to = '')",
                            tuple(_cand_ids),
                        ).fetchall()
                    valid_ids = {row[0] for row in valid_rows}
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
        # KG boost, CE reranking) — these add noise on keyword-specific
        # queries and cost seconds per search. FTS5 rank is the final rank.
        if mode not in ("fact_lookup", "fts"):
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

            if not light:
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

                # Text-based multi-hop traversal (no KG_ENABLED gate)
                _t0_tmh = time.time()
                try:
                    results = _text_multi_hop_traversal(
                        db, results, query, limit, repo_filter, category=category or None,
                    )
                except Exception as _tmh_exc:
                    _phase_inc("search.text_multi_hop", _tmh_exc)
                    logger.warning("text_multi_hop failed (degraded): %s", _tmh_exc)
                _record_phase_latency("search.text_multi_hop", _t0_tmh)

        # Phase 11: Reranking
        # fact_lookup and fts modes skip CE reranking — FTS5 rank is final.
        if mode in ("fact_lookup", "fts"):
            results_to_display = [
                (
                    r[0], r[1], r[2], r[3], r[4], r[5],
                    -r[5], None, None, None,
                    r[9] if len(r) > 9 else None,
                    r[10] if len(r) > 10 else None,
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
                    tenant_id=tenant_id,
                )
            except Exception as _rerank_exc:
                _phase_inc("search.rerank", _rerank_exc)
                logger.warning(
                    "rerank degraded (falling back to FTS-ranked results): %s", _rerank_exc
                )
                _search_ctr_weights = None
                if has_fitness and _effective_rerank:
                    results_to_display = [
                        cast(Any, (
                            r[0], r[1], r[2], r[3], r[4], r[5],
                            -r[5], None, None, None,
                            r[9] if len(r) > 9 else None,
                            r[10] if len(r) > 10 else None,
                            r[11] if len(r) > 11 else None,
                            None,
                        ))
                        for r in results
                    ]
                else:
                    results_to_display = [
                        (
                            r[0], r[1], r[2], r[3], r[4], r[5],
                            -r[5], None, None, None,
                            r[9] if len(r) > 9 else None,
                            r[10] if len(r) > 10 else None,
                            None,
                        )
                        for r in results
                    ]
            _record_phase_latency("rerank", _t0)

        # Phase 11.2: Temporal comparison — for queries that ask "which changed
        # first/last", "which was changed most recently", or "has X changed since...",
        # find the most recent session for each mentioned topic and compare timestamps.
        if results_to_display:
            _t0_tc = time.time()
            try:
                results_to_display = _temporal_compare(db, results_to_display, query, limit, repo_filter=repo_filter, as_of=as_of, tenant_id=tenant_id)
            except Exception as _tc_exc:
                _phase_inc("search.temporal_compare", _tc_exc)
                logger.debug("temporal_compare failed (degraded): %s", _tc_exc)
            _record_phase_latency("search.temporal_compare", _t0_tc)

        # Phase 11.3: Counting — for "how many times/how many distinct values" queries,
        # count distinct values across matching sessions and surface aggregate count.
        _t0_cnt = time.time()
        try:
            results_to_display = _counting_phase(db, results_to_display, query, limit, repo_filter=repo_filter, tenant_id=tenant_id)
        except Exception as _cnt_exc:
            _phase_inc("search.counting", _cnt_exc)
            logger.debug("counting phase failed (degraded): %s", _cnt_exc)
        _record_phase_latency("search.counting", _t0_cnt)

        # Phase 11.4: Conjoint Entity Retrieval — for compound queries ("What are X and Y?"),
        # ensure each conjunct independently secures top representation in results_to_display.
        _t0_conj = time.time()
        try:
            results_to_display = _conjoint_entity_phase(db, results_to_display, query, limit, tenant_id=tenant_id)
        except Exception as _conj_exc:
            _phase_inc("search.conjoint_entity", _conj_exc)
            logger.debug("conjoint_entity phase failed (degraded): %s", _conj_exc)
        _record_phase_latency("search.conjoint_entity", _t0_conj)

        # Phase 11.5: Entity-presence boost for inference queries.
        # When the query asks about a specific entity ("Would Caroline have X?"),
        # sessions containing that entity name should be promoted — the CE
        # reranker may demote them if the entity's vocabulary differs from
        # the query's concept keywords.
        # fact_lookup and fts modes skip this — FTS5 rank is already correct.
        if mode not in ("fact_lookup", "fts"):
            _entity, _ = _extract_inference_entity(query)
            if _entity and results_to_display:
                _entity_lower = _entity.lower()
                # M6 fix: cache config at function entry, not inside loop
                from search.config import get_search_config
                _entity_boost = get_search_config().entity_boost_factor
                for _idx, _rd in enumerate(results_to_display):
                    _content = (_rd[1] or "").lower() if len(_rd) > 1 else ""
                    if _entity_lower in _content:
                        # Boost final_score (index 6) by entity_boost_factor
                        _old_score = _rd[6] if len(_rd) > 6 else 0
                        if _old_score is not None:
                            _boosted = list(_rd[:7])
                            _boosted[6] = _old_score * _entity_boost
                            results_to_display[_idx] = tuple(_boosted) + tuple(_rd[7:])
                # Re-sort by final_score descending after boost
                results_to_display.sort(key=lambda x: x[6] if x[6] is not None else 0, reverse=True)

        # Phase 11.8: Contradiction Resolution Graph Engine (CRGE)
        if mode not in ("fact_lookup", "fts") and results_to_display:
            from search.phases.contradiction_engine import resolve_candidate_contradictions
            results_to_display = resolve_candidate_contradictions(results_to_display, query=query)
            results_to_display.sort(key=lambda x: float(x[6]) if len(x) > 6 and x[6] is not None else 0.0, reverse=True)

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
        # fact_lookup and fts modes skip postprocessing — FTS5 results already final.
        if not light and mode not in ("fact_lookup", "fts"):
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
                            f"metadata, 0.0, 0.0, 0, 1 "
                            f"FROM tenant_memories WHERE id IN ({','.join('?'*len(_new_ids))})",
                            tuple(_new_ids),
                        ).fetchall()
                        if _swm_extra:
                            # Shape each shared memory row to the canonical
                            # 13-element tuple: (id, content, source_file,
                            # tags, created, rank, final_score, fitness,
                            # importance, pinned, last_accessed, metadata,
                            # supersedes). No access_count in canonical shape.
                            _swm_display_rows = []
                            for _r in _swm_extra:
                                try:
                                    (
                                        _sid, _content, _sf, _tags, _created,
                                        _imp, _cat, _fit, _la, _meta,
                                    ) = _r[:10]
                                except ValueError:
                                    continue
                                _swm_display_rows.append(
                                    (
                                        _sid, _content, _sf, _tags, _created,
                                        0.0, 0.0, _fit, _imp, 0,
                                        _la, _meta,
                                        None,
                                    )
                                )
                            if _swm_display_rows:
                                results_to_display = (
                                    cast(list[Any], results_to_display)
                                    + cast(list[Any], _swm_display_rows)
                                )
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

            # M42 fix: shared results bypassed quality gates and limit.
            # Re-apply the caller's limit so the merged list does not
            # silently exceed the requested count.
            if len(results_to_display) > limit:
                results_to_display = results_to_display[:limit]
            if len(result_items) > limit:
                result_items = result_items[:limit]

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

        # CRDT staleness detection: sample a few results and check if
        # memory_field_crdt has newer values than memories.content.
        # This is a best-effort, log-only check — no mutations.
        try:
            from infra._lazy_imports import get_config as _crdt_chk_cfg
            if getattr(_crdt_chk_cfg(), "crdt_enabled", False) and result_items:
                _check_crdt_staleness(db, result_items[:5])
        except Exception as _crdt_chk_exc:
            logger.debug("CRDT staleness check skipped: %s", _crdt_chk_exc)

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
        _call_latencies = getattr(_phase_latencies_local, "latencies", {})
        if _call_latencies:
            result["phase_latencies"] = dict(_call_latencies)
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
            phase_latencies=dict(_call_latencies),
        )
        return result
    except Exception as e:
        _phase_inc("search.orchestrator", e)
        logger.exception("search_memories failed: %s", e)
        return {
            "results": [],
            "count": 0,
            "output": _err(ErrorCode.DB_ERROR, f"Search failed: {e}"),
        }
    finally:
        # Reset include_global flag to avoid leaking state across calls
        from infra.db import set_include_global
        set_include_global(False)
        if db is not None:
            try:
                safe_close_db(db)
            except Exception as e:
                logger.warning("safe_close_db failed: %s", e)


# Backward-compatible phase latency helper for test_observability.py.
def _record_phase_latency(name: str, start_time: float) -> None:
    """Record elapsed wall-clock latency for *name* into _phase_latencies."""
    elapsed_ms = (time.time() - start_time) * 1000.0
    # Write to per-call dict (thread-safe via thread isolation)
    call_lat = getattr(_phase_latencies_local, "latencies", None)
    if call_lat is not None:
        call_lat[name] = elapsed_ms
    # Also write to shared dict for backward compat (test_observability.py)
    with _phase_latencies_lock:
        _phase_latencies[name] = elapsed_ms
