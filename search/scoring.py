"""Scoring, fusion, and decay primitives for the search pipeline.

Extracted from search_pipeline.py (2026-06-20) as part of the god-module
decomposition. Contains:

- _reciprocal_rank_fusion: RRF fusion of multiple ranked lists (BB3)
- _temporal_decay_factor: Ebbinghaus-style temporal decay
- _apply_temporal_decay: LEGACY post-retrieval decay math utility (unit-tested
  only; the live post-rank behavior is the order-invariant ``temporal_decay``
  envelope field computed in search.enrichment under the RANK-FIRST LOCK)
- _apply_jaccard_surprise_penalty: LEGACY surprise penalty math utility
  (unit-tested only; the live post-rank behavior is the order-invariant
  ``jaccard_surprise`` envelope field in search.enrichment)
- _strong_match_float: float unambiguous FTS5 hits to the top (QB6)
- _compute_final_score: six-channel weighted scoring
- compute_channel_weights: CTR-feedback-driven weight tuning

The lazy config flags (``_TEMPORAL_DECAY_MODE``,
``_FORGETTING_CURVE_ENABLED``, ``_FORGETTING_CURVE_HALF_LIFE``,
``_TEMPORAL_DECAY_HALF_LIFE``) are resolved through search_pipeline's
module __getattr__ — the same source of truth as the rest of the system.

Behavior is identical to the inline versions. Re-exported from
search_pipeline for backward compat.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, cast

from search.config import get_search_config

HAS_NUMPY = True  # Corrected at runtime if numpy is unavailable.

if TYPE_CHECKING:
    import numpy as np
else:
    try:
        import numpy as np
    except ImportError:
        np = None  # type: ignore[assignment]
        HAS_NUMPY = False

logger = logging.getLogger(__name__)

_RRF_K_DEFAULT = 60  # Fallback; fusion.py reads hybrid_rrf_k from SearchConfig
_CONCEPT_BOOST = 1.35
_CONCEPT_MEMBERSHIP_BOOST = 1.20
_MAX_BOOST = 1.50
_CENTRALITY_BOOST_FACTOR = 1.25
_CENTRALITY_BOOST_MAX = 1.25
_RERANK_WEIGHTS = {
    "bm25": 0.40,
    "fitness": 0.20,
    "importance": 0.15,
    "pinned": 0.10,
    "recency": 0.10,
    "tag_match": 0.05,
}


def _get_rerank_weights() -> dict:
    return get_search_config().rerank_weights


def _get_query_type_weights() -> dict:
    """Return per-query-type weight overrides from memory_query_type_stats.

    Returns {query_type: {"bm25": ..., "fitness": ..., ...}} for types
    with ≥ MIN_INTERACTIONS learned weights.  Falls back to empty dict
    if no learned weights exist or the table is missing.
    """
    try:
        from infra._lazy_imports import connection_pool
        from infra.memory_common import GLOBAL_MEM_DIR

        db_path = str(GLOBAL_MEM_DIR / "memory.db")
        db = connection_pool.get(db_path, timeout=5.0)
        try:
            rows = db.execute(
                "SELECT query_type, weights_json, sample_count "
                "FROM memory_query_type_stats "
                "WHERE sample_count >= 10"
            ).fetchall()
            result = {}
            for qtype, weights_json, count in rows:
                try:
                    weights = json.loads(weights_json)
                    if isinstance(weights, dict) and all(
                        k in weights for k in _RERANK_WEIGHTS
                    ):
                        result[qtype] = weights
                except Exception:
                    continue
            return result
        finally:
            connection_pool.put(db)
    except Exception as e:
        logger.debug("_get_query_type_weights failed: %s", e)
        return {}


_MIN_INTERACTIONS = 10


def apply_query_type_weights(query_type: str) -> dict:
    """Return rerank weights, potentially overridden by learned CTR weights.

    When the query_type has ≥ MIN_INTERACTIONS learned weights in
    memory_query_type_stats, returns those.  Otherwise falls back to
    the global prior from _get_rerank_weights().

    The override must never degrade a known-good global prior — the cron
    job only writes weights when cross-validated AUC > 0.5.
    """
    type_weights = _get_query_type_weights()
    if query_type in type_weights:
        return dict(type_weights[query_type])
    return _get_rerank_weights()


_RERANK_TOKEN_RE = re.compile(r"[A-Za-z0-9#@+][A-Za-z0-9\-_/+#]{2,}")


def _get_strong_bm25_threshold() -> float:
    """Read strong BM25 threshold from config, falling back to 0.95."""
    return get_search_config().strong_bm25_threshold


_STRONG_BM25_THRESHOLD = None  # lazy: computed on first access
_STRONG_BM25_THRESHOLD_LOCK = threading.RLock()


def _normalize_bm25_ranks(results: list) -> list:
    """Normalize FTS5 BM25 ranks to [0, 1] per-query via min-max scaling.

    FTS5 returns bm25() as negative floats where lower (more negative) = better.
    On a shared index, IDF varies wildly across queries — a rare-term query
    produces ranks in [-30, -5] while a common-term query produces [-2, -0.1].
    Without normalization, the sigmoid 1/(1+exp(rank)) compresses most ranks
    into [0.45, 0.55], making BM25 nearly weightless in the final score.

    This function rescales ranks to [0, 1] (best=1.0, worst=0.0) before the
    sigmoid conversion, so BM25 contributes meaningful discrimination regardless
    of the absolute rank magnitude.
    """
    if not results or len(results) < 2:
        return results
    ranks = []
    for r in results:
        try:
            ranks.append(float(r[5]))
        except (TypeError, IndexError):
            ranks.append(0.0)
    min_r = min(ranks)
    max_r = max(ranks)
    span = max_r - min_r
    if span < 1e-9:
        return results
    normalized = []
    for r, rank in zip(results, ranks):
        norm = (rank - min_r) / span  # 0.0 = best (most negative FTS5 rank), 1.0 = worst
        # Convert to a scaled rank that sigmoid maps to a wider range:
        # norm=0.0 (best) → rank=-5 (bm25~0.993), norm=1.0 (worst) → rank=5 (bm25~0.007)
        scaled_rank = -5.0 + 10.0 * norm
        new_r = list(r)
        new_r[5] = scaled_rank
        normalized.append(tuple(new_r))
    return normalized

_CTR_WEIGHTS_CACHE: Optional[tuple[float, Any, bool]] = None
_CTR_WEIGHTS_CACHE_LOCK = threading.RLock()
_CTR_WEIGHTS_TTL = 300  # 5 minutes


def _sp_lazy(name: str, default: object = None) -> object:
    """Read a lazy config flag from search_pipeline's __getattr__.

    The lazy config resolver lives in search_pipeline. This helper looks
    up the flag there, falling back to ``default`` if it's not set
    (e.g. during testing or before the config singleton is initialized).
    """
    try:
        import search_pipeline as sp
        return getattr(sp, name, default)
    except ImportError:
        return default


def _get_rerank_half_life_days() -> float:
    """Resolve rerank_half_life_days from config; falls back to 180.0."""
    return get_search_config().rerank_half_life_days


_RERANK_HALF_LIFE_DAYS = None  # lazy: computed on first access


def _reciprocal_rank_fusion(
    ranked_lists, k: int = _RRF_K_DEFAULT, weights: Optional[list[float]] = None
) -> dict:
    """BB3: combine multiple ranked result lists via reciprocal rank fusion.

    Args:
        ranked_lists: iterable of lists. Each inner list is a sequence of
            doc_ids (or (doc_id, score) tuples) ordered by descending
            relevance.  Ties are broken by list order.
        k: dampening constant (default 60). Larger k reduces the influence
            of top ranks; smaller k amplifies them.
        weights: optional per-channel weight multipliers. Must be the same
            length as ``ranked_lists``.  ``None`` or all-1.0 gives equal
            weight (identical to pre-W1 behavior).

    Returns:
        dict mapping doc_id → float rrf score. Documents that appear in
        multiple lists get summed weighted scores.
    """
    lists = list(ranked_lists)
    if weights is None:
        weights = [1.0] * len(lists)
    fused: dict = {}
    for lst, w in zip(lists, weights):
        for rank, item in enumerate(lst):
            if isinstance(item, tuple):
                doc_id = item[0]
            else:
                doc_id = item
            fused[doc_id] = fused.get(doc_id, 0.0) + w / (k + rank + 1)
    return fused


def _temporal_decay_factor(
    created: str,
    now_ts: Optional[float] = None,
    last_accessed: Optional[str] = None,
    as_of: Optional[float] = None,
    half_life: Optional[float] = None,
) -> float:
    """Compute a temporal decay factor for a note.

    Sprint 5: ``as_of`` is a time-travel anchor.  When set, it replaces
    ``time.time()`` for computing the note's age, so decay reflects what
    the note's recency would have been at that past (or future) moment.

    When ``MEMORY_FORGETTING_CURVE=1``, uses last_accessed (Ebbinghaus
    forgetting curve).  Otherwise, uses created timestamp (standard
    temporal decay).  Returns a value in [0, 1] where 1 = brand new,
    0 = very old.
    """
    # Resolve config values once (not per-call via _sp_lazy sys.modules lookup)
    _td_mode = _sp_lazy("_TEMPORAL_DECAY_MODE", "exponential")
    _fc_enabled = _sp_lazy("_FORGETTING_CURVE_ENABLED", False)
    _fc_half_life = _sp_lazy("_FORGETTING_CURVE_HALF_LIFE", 30)

    if _td_mode == "off":
        return 1.0
    if as_of is not None:
        now_ts = as_of
    if now_ts is None:
        now_ts = time.time()

    # Forgetting curve: decay based on last_accessed
    if _fc_enabled and last_accessed:
        try:
            la_dt = datetime.fromisoformat(last_accessed)
            if la_dt.tzinfo is None:
                la_dt = la_dt.replace(tzinfo=timezone.utc)
            la_ts = la_dt.timestamp()
            age_days = max(0.0, (now_ts - la_ts) / 86400.0)
        except (ValueError, TypeError):
            # Fall through to created-based decay
            pass
        else:
            _fc_hl = float(cast(float, _fc_half_life))
            if _fc_hl <= 0:
                return 1.0  # skip decay to avoid division by zero
            if _td_mode == "linear":
                return max(
                    0.0, 1.0 - float(age_days) / (3.0 * _fc_hl)
                )
            return float(0.5 ** (float(age_days) / _fc_hl))

    # Standard decay based on created timestamp
    if not created:
        return 1.0
    try:
        c_dt = datetime.fromisoformat(created)
        if c_dt.tzinfo is None:
            c_dt = c_dt.replace(tzinfo=timezone.utc)
        c_ts = c_dt.timestamp()
        age_days = max(0.0, (now_ts - c_ts) / 86400.0)
    except (ValueError, TypeError):
        return 1.0
    _td_half_life = _sp_lazy("_TEMPORAL_DECAY_HALF_LIFE", 180)
    hl = half_life if half_life is not None else float(cast(float, _td_half_life))
    if hl <= 0:
        return 1.0  # skip decay to avoid division by zero
    if _td_mode == "linear":
        return max(
            0.0,
            1.0
            - float(age_days)
            / (3.0 * hl),
        )
    return float(0.5 ** (
        float(age_days) / hl
    ))


def _apply_jaccard_surprise_penalty(scored_results: list, query: str) -> list:
    """LEGACY math utility — do not call from the live search pipeline.

    The live post-rank behavior lives in ``search.enrichment._jaccard_factor``,
    which attaches a ``jaccard_surprise`` envelope field to each result item
    under the RANK-FIRST LOCK (PR1.1) without mutating the ranking
    ``final_score``. This function still multiplies ``r[6]`` directly and is
    retained only so the surprise math stays unit-testable in isolation.

    B19 fix: the original temporal decay is purely time-based. The
    neural-forget curve adds a *surprise* term — how unexpected this
    note was for the current query — so notes that the user has
    recently accessed for *similar* queries decay less than notes
    that were last accessed for unrelated queries.

    This is a lightweight proxy: it uses Jaccard distance between
    the current query and the last-accessed query (from
    adaptive_retention) as the surprise signal. When surprise is
    high (low overlap), the note is "forgotten faster."

    P1-10 fix (2026-06-24): replaced the N+1 pattern (``_galq(note_id)``
    called once per result) with a single batch call
    (``get_last_access_queries_batch``) that resolves all note_ids in
    one query against ``memory_audit_log``.

    Best-effort: failures degrade to no-op.
    """
    try:
        from adaptive_retention import get_last_access_queries_batch
    except ImportError:
        return scored_results

    if not scored_results or not query:
        return scored_results
    try:
        q_tokens = set(re.findall(r'\w+', query.lower()))
    except Exception:
        logger.warning("Failed to tokenize query for surprise scoring")
        return scored_results
    if not q_tokens:
        return scored_results

    # P1-10 fix: batch-collect all note_ids and resolve last-access
    # queries in a single DB round trip instead of one query per row.
    note_ids = [r[0] for r in scored_results if r and r[0]]
    last_queries = get_last_access_queries_batch(note_ids)

    modified = []
    for r in scored_results:
        note_id = r[0]
        if not note_id:
            modified.append(r)
            continue
        last_q = last_queries.get(note_id)
        if not last_q:
            modified.append(r)
            continue
        try:
            last_tokens = set(re.findall(r'\w+', last_q.lower()))
        except Exception:
            logger.warning("Failed to tokenize last query for surprise scoring")
            modified.append(r)
            continue
        if not last_tokens:
            modified.append(r)
            continue
        inter = len(q_tokens & last_tokens)
        union = len(q_tokens | last_tokens)
        jaccard = inter / union if union > 0 else 0.0
        # surprise = 1 - jaccard (high surprise = low overlap)
        surprise = 1.0 - jaccard
        # Apply gentle surprise penalty (capped at 10% to avoid
        # overriding relevance signals)
        penalty = 1.0 - 0.1 * surprise
        try:
            (
                note_id_r,
                content,
                source_file,
                tags_json,
                created,
                rank,
                final_score,
                fitness,
                importance,
                pinned,
            ) = r[:10]
            _ = r[10] if len(r) > 10 else None
            adjusted = final_score * penalty
            new_r = list(r)
            if len(new_r) >= 7:
                new_r[6] = adjusted
            modified.append(tuple(new_r))
        except Exception:
            logger.warning("Failed to apply surprise penalty to result")
            modified.append(r)
    return modified


_UNSET = object()

def _apply_temporal_decay(
    scored_results: list,
    decay_weight: float = _UNSET,  # type: ignore[assignment]
    as_of: Optional[float] = None,
) -> list:
    """LEGACY math utility — do not call from the live search pipeline.

    The live post-rank behavior lives in ``search.enrichment._temporal_factor``,
    which attaches a ``temporal_decay`` envelope field to each result item
    under the RANK-FIRST LOCK (PR1.1) without mutating the ranking ``final_score``.
    This function still multiplies ``r[6]`` directly and is retained only so the
    decay math stays unit-testable in isolation; the pipeline path must NOT call
    it. See eval/test_search_rank_lock.py which asserts the pipeline does not.

    Sprint 5: ``as_of`` is forwarded to ``_temporal_decay_factor`` so
    recency is calculated relative to the time-travel anchor instead of
    ``time.time()``.

    Multiplies each result's final_score by (1 - decay_weight + decay_weight * decay_factor).
    This boosts recent notes and gently penalizes old ones without
    overriding the relevance-based ranking entirely.
    When MEMORY_FORGETTING_CURVE=1, uses last_accessed for Ebbinghaus-style decay.
    decay_weight is read from config (temporal_decay_weight) when not explicitly passed.
    """
    if decay_weight is _UNSET:
        try:
            from search.config import get_search_config

            decay_weight = get_search_config().temporal_decay_weight
        except Exception as e:
            logger.warning("Unhandled exception in _apply_temporal_decay: %s", e)
            decay_weight = 0.15
    if _sp_lazy("_TEMPORAL_DECAY_MODE", "exponential") == "off" or decay_weight <= 0:
        return scored_results
    now_ts = time.time() if as_of is None else as_of
    modified = []
    for r in scored_results:
        created = r[4]
        final_score = r[6]
        last_accessed = r[10] if len(r) > 10 else None
        decay = _temporal_decay_factor(
            created, now_ts, last_accessed=last_accessed, as_of=as_of
        )
        adjusted = final_score * (1.0 - decay_weight + decay_weight * decay)
        new_r = list(r)
        new_r[6] = adjusted
        modified.append(tuple(new_r))
    return modified


def _strong_match_float(rows):
    """Bucket ``rows`` so unambiguous FTS5 hits come first relative to the
    rest of the list. Within each bucket, the original relative order is
    preserved.

    The threshold is configurable via ``strong_bm25_threshold`` in the
    search config (default 0.95). This is a deliberate design choice:
    the sigmoid 1/(1+exp(rank)) maps FTS5 rank to a probability-like
    [0,1] scale, and 0.95 selects documents where the raw FTS5 rank is
    decisively strong relative to the corpus. The threshold is corpus-
    independent because the sigmoid normalizes across index sizes.
    """
    global _STRONG_BM25_THRESHOLD
    if not rows:
        return rows
    strong, rest = [], []
    # M9 fix: double-checked locking for thread-safe lazy init
    if _STRONG_BM25_THRESHOLD is None:
        with _STRONG_BM25_THRESHOLD_LOCK:
            if _STRONG_BM25_THRESHOLD is None:
                _STRONG_BM25_THRESHOLD = _get_strong_bm25_threshold()
    for r in rows:
        try:
            _r = float(r[5]) if len(r) > 5 else 0.0  # raw fts5 rank
        except (TypeError, ValueError):
            _r = 0.0
        _r = max(-60.0, min(60.0, _r))
        _bm = 1.0 / (1.0 + math.exp(_r))
        (strong if _bm >= _STRONG_BM25_THRESHOLD else rest).append(r)
    if not strong:
        return rows
    return strong + rest


def _compute_final_score(ctx) -> float:
    """Combine six retrieval channels into a single final score.

    Channels (default weights, sum to 1.0):
        bm25:       0.40  text relevance, FTS5 rank negated
        fitness:    0.20  success/recency score from ARC
        importance: 0.15  user-set importance 1-5, /5
        pinned:     0.10  always-on boost, scaled by boost_pinned
        recency:    0.10  temporal decay factor from created/accessed timestamps
        tag_match:  0.05  fraction of query tokens present in tags

    ``now_ts`` is injectable for deterministic tests.
    """
    weights = ctx.weights
    if weights is None:
        weights = _get_rerank_weights()
    now_ts = ctx.now_ts
    if now_ts is None:
        now_ts = time.time()
    # Defensively clamp rank to keep math.exp(rank) finite. SQLite FTS5
    # returns bm25 as a negative rank, but defensively bound the input
    # to [-60, 60] so a stray positive rank or a numeric overflow
    # elsewhere can't produce inf/NaN that would poison final_score
    # and propagate through sort/return.
    try:
        rank = float(ctx.rank)
    except (TypeError, ValueError):
        rank = 0.0
    rank = max(-60.0, min(60.0, rank))
    bm25_score = 1.0 / (1.0 + math.exp(rank))
    fitness_score = ctx.fitness if ctx.fitness is not None else 0.5
    importance_val = ctx.importance if ctx.importance is not None else 3
    importance_normalized = importance_val / 5.0
    pinned_bonus = 1.0 if ctx.pinned and ctx.boost_pinned else 0.0
    tag_match = 0.0
    query_tokens = ctx.query_tokens if ctx.query_tokens is not None else {
        t.lower() for t in _RERANK_TOKEN_RE.findall(ctx.query) if len(t) >= 3
    }
    if query_tokens:
        try:
            tags_list = json.loads(ctx.tags_json) if ctx.tags_json else []
        except Exception:
            logger.warning("Failed to parse tags JSON for tag scoring")
            tags_list = []
        if tags_list:
            # Normalize tags through the same regex as query tokens so tags
            # like "node.js", ".net", "react.ts" match their query counterparts.
            tag_tokens = set()
            for t in tags_list:
                if not isinstance(t, str):
                    continue
                for token in _RERANK_TOKEN_RE.findall(t):
                    if len(token) >= 3:
                        tag_tokens.add(token.lower())
            if tag_tokens:
                hits = len(query_tokens & tag_tokens)
                tag_match = min(1.0, hits / max(1, len(query_tokens)))
    # Calculate recency score using temporal decay factor
    created = getattr(ctx, "created", None) or ""
    last_accessed = getattr(ctx, "last_accessed", None)
    recency_score = _temporal_decay_factor(
        created, now_ts, last_accessed=last_accessed, as_of=now_ts
    )
    # A3.3: discount inferred (is_entailed=1) fact scores so directly
    # observed facts outrank derived knowledge.  is_entailed defaults to
    # 0 (direct fact) when absent on memory rows.
    _entailment_factor = 0.8 if getattr(ctx, "is_entailed", None) == 1 else 1.0
    # Use the per-query-type recency channel weight by default so the
    # six-channel weights stay authoritative.  The only legitimate override
    # is an explicit ScoreContext.recency_weight of exactly 0.0, which
    # suppresses recency entirely; any other value is ignored.
    # M12 fix: use _RERANK_WEIGHTS as single source of truth (no duplicate _defaults dict).
    _recency_weight = float(weights.get("recency", _RERANK_WEIGHTS["recency"]))
    _ctx_rw = getattr(ctx, "recency_weight", None)
    if _ctx_rw is not None and float(_ctx_rw) == 0.0:
        _recency_weight = 0.0
    # M13 fix: when recency is zeroed, redistribute its weight proportionally
    # across the other non-pinned channels so total weight stays at 1.0.
    _bm25_w = float(weights.get("bm25", _RERANK_WEIGHTS["bm25"]))
    _fitness_w = float(weights.get("fitness", _RERANK_WEIGHTS["fitness"]))
    _importance_w = float(weights.get("importance", _RERANK_WEIGHTS["importance"]))
    _pinned_w = float(weights.get("pinned", _RERANK_WEIGHTS["pinned"]))
    _tag_w = float(weights.get("tag_match", _RERANK_WEIGHTS["tag_match"]))
    if _recency_weight == 0.0:
        _non_pinned_sum = _bm25_w + _fitness_w + _importance_w + _tag_w
        if _non_pinned_sum > 0:
            _scale = (1.0 - _pinned_w) / _non_pinned_sum
            _bm25_w *= _scale
            _fitness_w *= _scale
            _importance_w *= _scale
            _tag_w *= _scale
    raw = (
        _bm25_w * bm25_score
        + _fitness_w * fitness_score
        + _importance_w * importance_normalized
        + _pinned_w * pinned_bonus
        + _tag_w * tag_match
        + _recency_weight * recency_score
    )
    # Neural retention blend: memories.score (from neural_forget) is a
    # retention probability in [0, 1].  Blend it multiplicatively so
    # high-retention memories get a gentle boost and low-retention
    # memories get discounted — without dominating the signal channels.
    forget = getattr(ctx, "forget_score", None)
    if forget is not None:
        try:
            _forget = float(forget)
            _forget = max(0.0, min(1.0, _forget))
            # Blend: 85% raw score + 15% retention-scaled score.
            # At forget=1.0 → no change; at forget=0.0 → 15% discount.
            raw = raw * (0.85 + 0.15 * _forget)
        except (TypeError, ValueError):
            pass
    return raw * _entailment_factor


# Cached exploration mode (resolved once, not per-search)
_EXPLORATION_MODE = None
_EXPLORATION_MODE_RESOLVED = False
_exploration_lock = threading.Lock()

def _apply_exploration(cached_stats) -> Optional[dict]:
    global _EXPLORATION_MODE, _EXPLORATION_MODE_RESOLVED
    if not HAS_NUMPY:
        return cast(dict, cached_stats[2]) if cached_stats else None
    if cached_stats is None:
        return None
    alphas, betas, expected = cached_stats
    if not _EXPLORATION_MODE_RESOLVED:
        with _exploration_lock:
            if not _EXPLORATION_MODE_RESOLVED:
                try:
                    from config import get_config
                    cfg = get_config()
                    _EXPLORATION_MODE = os.environ.get("MEMORY_EXPLORATION_MODE", getattr(cfg, "exploration_mode", "off")).lower()
                except Exception as e:
                    logger.warning("Unhandled exception in _apply_exploration: %s", e)
                    _EXPLORATION_MODE = os.environ.get("MEMORY_EXPLORATION_MODE", "off").lower()
                _EXPLORATION_MODE_RESOLVED = True
    mode = _EXPLORATION_MODE

    if mode == "thompson":
        sampled = {}
        for ch in _RERANK_WEIGHTS:
            a = alphas.get(ch, 1.0)
            b = betas.get(ch, 1.0)
            sampled[ch] = float(np.random.beta(max(0.1, a), max(0.1, b)))
        s = sum(sampled.values())
        if s > 0:
            return {ch: v / s for ch, v in sampled.items()}
        return cast(dict, expected)

    elif mode == "epsilon_greedy":
        epsilon = float(os.environ.get("MEMORY_CTR_EPSILON", "0.1"))
        if np.random.random() < epsilon:
            if np.random.random() < 0.5:
                return _RERANK_WEIGHTS
            else:
                raw_random = {ch: float(np.random.random()) for ch in _RERANK_WEIGHTS}
                s = sum(raw_random.values())
                return {ch: v / s for ch, v in raw_random.items()} if s > 0 else _RERANK_WEIGHTS
        return cast(dict, expected)

    return cast(dict, expected)




def compute_channel_weights(db_path: Path) -> Optional[dict]:
    """Compute channel weights from CTR feedback data."""
    return get_ctr_weights(db_path)


def clear_scoring_caches() -> None:
    """Clear CTR weights cache."""
    global _CTR_WEIGHTS_CACHE
    with _CTR_WEIGHTS_CACHE_LOCK:
        _CTR_WEIGHTS_CACHE = None



def get_ctr_weights(db_path: Any) -> Optional[dict[str, float]]:
    """Compute empirical channel weights from recent CTR feedback.

    Queries ``memory_ctr_feedback`` to compute per-channel
    performance multipliers, returning a dict of normalized weights.
    Falls back to static defaults if feedback is sparse (<10 queries)
    or the table is missing.

    Cached for 1 hour to avoid running heavy feedback-aggregation
    query runs per search.
    """
    # CTR tuning is on by default.  Set MEMORY_CTR_TUNING=0 to opt out.
    # If the gating env var changed since the cache was populated, drop
    # the cache. Otherwise we'd return a stale result from before the
    # flag flipped. (Discovered during audit; testing tooling toggles
    # the env var between sessions.)
    db_path_str = str(db_path)
    st_ino = 0
    if os.path.exists(db_path_str):
        try:
            st_ino = os.stat(db_path_str).st_ino
        except OSError:
            pass

    with _CTR_WEIGHTS_CACHE_LOCK:
        env_val = os.environ.get("MEMORY_CTR_TUNING", "1").strip()
        ctr_enabled = env_val != "0"
        global _CTR_WEIGHTS_CACHE

        if _CTR_WEIGHTS_CACHE is not None:
            if len(_CTR_WEIGHTS_CACHE) == 5:
                ts, cached, cached_env, cached_db, cached_ino = _CTR_WEIGHTS_CACHE
                if cached_db != db_path_str or cached_ino != st_ino or cached_env != ctr_enabled or time.time() - ts >= _CTR_WEIGHTS_TTL:
                    _CTR_WEIGHTS_CACHE = None
            else:
                _CTR_WEIGHTS_CACHE = None

        if _CTR_WEIGHTS_CACHE is not None:
            ts, cached, _cached_env, _cached_db, _cached_ino = _CTR_WEIGHTS_CACHE
            return _apply_exploration(cached)

        if not ctr_enabled:
            _CTR_WEIGHTS_CACHE = (time.time(), None, ctr_enabled, db_path_str, st_ino)
            return None

    try:
        from infra._lazy_imports import connection_pool

        db = connection_pool.get(str(db_path), timeout=5.0)
        try:
            # L6 fix: use PRAGMA table_info instead of full sqlite_master scan
            _has_ctr = db.execute(
                "SELECT 1 FROM PRAGMA table_info('memory_ctr_feedback') LIMIT 1"
            ).fetchone()
            if not _has_ctr:
                with _CTR_WEIGHTS_CACHE_LOCK:
                    _CTR_WEIGHTS_CACHE = (time.time(), None, ctr_enabled, db_path_str, st_ino)
                return None

            from config import get_config

            _cfg = get_config()
            cutoff = time.time() - _cfg.ctr_data_window_days * 86400
            rows = db.execute(
                "SELECT query_id, ranking_params, "
                "SUM(CASE WHEN clicked_at IS NOT NULL THEN 1 ELSE 0 END) as clicks, "
                "SUM(CASE WHEN dismissed_at IS NOT NULL THEN 1 ELSE 0 END) as dismissals "
                "FROM memory_ctr_feedback "
                "WHERE returned_at > ? "
                "GROUP BY query_id "
                "HAVING (clicks + dismissals) > 0",
                (cutoff,),
            ).fetchall()

            if len(rows) < 10:
                with _CTR_WEIGHTS_CACHE_LOCK:
                    _CTR_WEIGHTS_CACHE = (time.time(), None, ctr_enabled, db_path_str, st_ino)
                return None

            alphas = {ch: 1.0 for ch in _RERANK_WEIGHTS}
            betas = {ch: 1.0 for ch in _RERANK_WEIGHTS}
            channel_sums: dict = {k: 0.0 for k in _RERANK_WEIGHTS}
            total_weight = 0.0

            for _qid, ranking_params_json, clicks, dismissals in rows:
                ctr = clicks / (clicks + dismissals)
                try:
                    params = (
                        json.loads(ranking_params_json) if ranking_params_json else {}
                    )
                    row_weights = params.get("weights", _RERANK_WEIGHTS)
                except Exception:
                    logger.warning("Failed to parse CTR ranking params")
                    row_weights = _RERANK_WEIGHTS
                for ch in _RERANK_WEIGHTS:
                    w = row_weights.get(ch, _RERANK_WEIGHTS[ch])
                    alphas[ch] += clicks * w
                    betas[ch] += dismissals * w
                    channel_sums[ch] += ctr * w
                total_weight += ctr

            if total_weight <= 0:
                with _CTR_WEIGHTS_CACHE_LOCK:
                    _CTR_WEIGHTS_CACHE = (time.time(), None, ctr_enabled, db_path_str, st_ino)
                return None

            adjusted = {ch: channel_sums[ch] / total_weight for ch in _RERANK_WEIGHTS}
            s = sum(adjusted.values())
            if s > 0:
                adjusted = {ch: v / s for ch, v in adjusted.items()}
            else:
                adjusted = _RERANK_WEIGHTS

            stats = (alphas, betas, adjusted)
            with _CTR_WEIGHTS_CACHE_LOCK:
                _CTR_WEIGHTS_CACHE = (time.time(), stats, ctr_enabled, db_path_str, st_ino)
            return _apply_exploration(stats)
        finally:
            connection_pool.put(db)
    except Exception:
        logger.warning("Failed to compute CTR weights from feedback")
        with _CTR_WEIGHTS_CACHE_LOCK:
            _CTR_WEIGHTS_CACHE = (time.time(), None, ctr_enabled, db_path_str, st_ino)
        return None


_W_INPUT_DIM = 6   # query embedding dimension
_W_HIDDEN = 8      # hidden state dimension
_W_TOTAL = _W_HIDDEN + 1 + _W_HIDDEN * _W_INPUT_DIM + 1  # 58


class TemporalAttentionModel:
    """Lightweight SSM-style temporal attention model for note recency scoring.

    Weights layout (58 elements total):
        W_readout[0:8]    -- linear readout head
        b_readout[8]      -- readout bias
        W_input[8:56]     -- 8 by 6 input-weight matrix (row-major)
        b_input[56:57]    -- input bias
    """


    def __init__(self, weights=None):
        from config import get_config

        if not getattr(get_config(), 'temporal_ssm_enabled', False):
            raise RuntimeError(
                "TemporalAttentionModel disabled (MEMORY_TEMPORAL_SSM_ENABLED=0)"
            )
        self.has_learned_weights = False
        if not HAS_NUMPY:
            return
        self.W_readout = np.zeros(_W_HIDDEN, dtype=np.float64)
        self.W_input = np.zeros((_W_HIDDEN, _W_INPUT_DIM), dtype=np.float64)
        self.b_readout = 0.0
        self.b_input = 0.0
        self._hidden: dict[str, np.ndarray] = {}
        self._last_access_ts: dict[str, float] = {}
        if weights is not None:
            w = np.asarray(weights, dtype=np.float64)
            if w.size != _W_TOTAL:
                raise ValueError(
                    f"TemporalAttentionModel expects {_W_TOTAL} weights, got {w.size}"
                )
            self.has_learned_weights = True
            self.W_readout = w[0:8].copy()
            self.b_readout = float(w[8])
            flat_input = w[9:57].copy()
            self.W_input = flat_input.reshape(_W_HIDDEN, _W_INPUT_DIM)
            self.b_input = float(w[57])

    def observe(
        self,
        note_id: str,
        query_emb: np.ndarray,
        clicked: bool = False,
        dismissed: bool = False,
        hours_since_access: float = 0.0,
    ) -> None:
        if not HAS_NUMPY:
            return
        h = np.zeros(_W_HIDDEN, dtype=np.float64)
        if self.has_learned_weights:
            q = np.asarray(query_emb, dtype=np.float64).flatten()
            if q.shape[0] != _W_INPUT_DIM:
                q = np.pad(q, (0, max(0, _W_INPUT_DIM - q.shape[0])))[:_W_INPUT_DIM]
            decay = math.exp(-max(0.0, hours_since_access) / 168.0)
            h = np.tanh(self.W_input @ q + self.b_input) * decay
            if clicked:
                h += 0.1 * self.W_readout
            elif dismissed:
                h -= 0.1 * self.W_readout
        self._hidden[note_id] = h
        self._last_access_ts[note_id] = time.time()

    def score(self, note_id: str) -> float:
        if not HAS_NUMPY or not self.has_learned_weights or note_id not in self._hidden:
            return 0.5
        h = self._hidden[note_id]
        raw = float(np.dot(self.W_readout, h)) + self.b_readout
        return float(np.tanh(raw) * 0.5 + 0.5)

    def prune(self, older_than_hours: float = 720) -> int:
        cutoff = time.time() - older_than_hours * 3600
        stale = [
            nid for nid, ts in self._last_access_ts.items() if ts < cutoff
        ]
        for nid in stale:
            self._hidden.pop(nid, None)
            self._last_access_ts.pop(nid, None)
        return len(stale)

    def to_config_str(self) -> str:
        parts = [f"{v:.6f}" for v in self.W_readout]
        parts.append(f"{self.b_readout:.6f}")
        flat_input = self.W_input.ravel()
        parts.extend(f"{v:.6f}" for v in flat_input)
        parts.append(f"{self.b_input:.6f}")
        return ",".join(parts)

    @classmethod
    def from_config(cls) -> "TemporalAttentionModel":
        """Build a model, loading trained weights from config if present.

        ``temporal_ssm_enabled`` must be on (the model is opt-in; __init__
        raises otherwise). When ``temporal_ssm_weights`` is populated by
        cron_train_temporal_ssm the learned weights are used; otherwise a
        zero-weight instance is returned whose ``score()`` is neutral (0.5)
        until trained.
        """
        from config import get_config

        raw = getattr(get_config(), "temporal_ssm_weights", "")
        weights = None
        if raw:
            try:
                parts = [float(x) for x in raw.split(",")]
                if len(parts) == _W_TOTAL:
                    weights = parts
            except (ValueError, TypeError):
                logger.warning("TemporalAttentionModel.from_config: bad weights, using zeros")
        return cls(weights=weights)


# ---------------------------------------------------------------------------
# Temporal SSM recency reranker (gated by temporal_ssm_enabled).
#
# The model is only ever instantiated when the feature flag is on.  With
# zero (untrained) weights its score() is exactly 0.5, so the reranker is a
# no-op until cron_train_temporal_ssm has written temporal_ssm_weights —
# this keeps the feature inert (and the RANK-FIRST LOCK respected) until a
# trained checkpoint exists.
# ---------------------------------------------------------------------------
_SSM_MODEL = None
_SSM_MODEL_LOCK = threading.Lock()
_SSM_BLEND = 0.25  # max ±12.5% swing on final_score when fully trained


def _get_ssm_model() -> Optional["TemporalAttentionModel"]:
    global _SSM_MODEL
    if _SSM_MODEL is not None:
        return _SSM_MODEL
    try:
        from config import get_config

        if not getattr(get_config(), "temporal_ssm_enabled", False):
            return None
    except Exception as e:
        logger.debug("_get_ssm_model flag check failed: %s", e)
        return None
    with _SSM_MODEL_LOCK:
        if _SSM_MODEL is None:
            _SSM_MODEL = TemporalAttentionModel.from_config()
    return _SSM_MODEL


def reset_ssm_model() -> None:
    """Drop the cached SSM model (after config reload or in tests)."""
    global _SSM_MODEL
    with _SSM_MODEL_LOCK:
        _SSM_MODEL = None


def _ssm_input_vector(access_count, query_surprise, importance, fitness, recency_penalty):
    """6-dim input matching the training-time feature layout.

    (access_signal, query_surprise, importance_norm, fitness,
    recency_penalty, zero_pad). The 6th slot is padding the model learns to
    ignore, keeping the layout identical between training and inference so
    trained weights transfer.
    """
    access_signal = min((access_count or 1) / 20.0, 1.0)
    importance_norm = (importance or 3) / 5.0
    return [
        access_signal,
        float(query_surprise),
        importance_norm,
        float(fitness if fitness is not None else 0.5),
        min(float(recency_penalty), 1.0),
        0.0,
    ]


def _apply_temporal_ssm_rerank(query, scored_results, as_of=None):
    """Multiply each result's final_score (r[6]) by the SSM recency nudge.

    Placed as the final gated reranker stage (after CE / late-interaction /
    LTR) so the trained temporal signal has maximum impact on ranking.  The
    nudge is in [0, 1]; with untrained weights it is 0.5 → blend factor 1.0
    (no ranking effect), so enabling the flag before training is safe.

    Expected tuple layout (same as the scoring loop):
        r[0]=note_id, r[1]=content, r[6]=final_score, r[7]=fitness,
        r[8]=importance, r[10]=last_accessed, r[12]=access_count
    """
    model = _get_ssm_model()
    if model is None or not scored_results or not HAS_NUMPY:
        return scored_results
    now_ts = time.time() if as_of is None else as_of
    q_tokens = set(re.findall(r"\w+", (query or "").lower()))
    modified = []
    for r in scored_results:
        if not r or len(r) <= 6 or r[6] is None:
            modified.append(r)
            continue
        note_id = r[0]
        content = r[1] if len(r) > 1 else ""
        fitness = r[7] if len(r) > 7 else 0.5
        importance = r[8] if len(r) > 8 else 3
        last_accessed = r[10] if len(r) > 10 else None
        access_count = r[12] if len(r) > 12 else 1

        recency_days = 0.0
        if last_accessed:
            try:
                ts = datetime.fromisoformat(last_accessed).timestamp()
                recency_days = max(0.0, (now_ts - ts) / 86400.0)
            except (ValueError, TypeError):
                pass
        recency_penalty = min(recency_days / 365.0, 1.0)

        query_surprise = 0.5
        if q_tokens and content:
            c_tokens = set(re.findall(r"\w+", content.lower()))
            if c_tokens:
                union = len(q_tokens | c_tokens)
                query_surprise = 1.0 - (len(q_tokens & c_tokens) / union if union else 0.0)

        q = _ssm_input_vector(access_count, query_surprise, importance, fitness, recency_penalty)
        try:
            model.observe(note_id, np.array(q, dtype=float), hours_since_access=recency_days * 24.0)
            nudge = model.score(note_id)
        except Exception as e:
            logger.debug("_apply_temporal_ssm_rerank per-row failed: %s", e)
            modified.append(r)
            continue

        blend = 1.0 + _SSM_BLEND * (nudge - 0.5)
        blend = max(0.5, min(1.5, blend))
        new_r = list(r)
        new_r[6] = float(r[6]) * blend
        modified.append(tuple(new_r))
    return modified
