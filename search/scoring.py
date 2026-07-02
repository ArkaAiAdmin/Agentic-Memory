"""Scoring, fusion, and decay primitives for the search pipeline.

Extracted from search_pipeline.py (2026-06-20) as part of the god-module
decomposition. Contains:

- _reciprocal_rank_fusion: RRF fusion of multiple ranked lists (BB3)
- _temporal_decay_factor: Ebbinghaus-style temporal decay
- _apply_temporal_decay: post-retrieval decay modifier
- _apply_neural_forget_curve: surprise-based re-ranking (B19)
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
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, cast

import numpy as np

logger = logging.getLogger(__name__)

_RRF_K = 60
_RERANK_WEIGHTS = {
    "bm25": 0.4,
    "fitness": 0.2,
    "importance": 0.15,
    "pinned": 0.1,
    "recency": 0.1,
    "tag_match": 0.05,
}


def _get_rerank_weights() -> dict:
    try:
        from infra._lazy_imports import get_config

        raw = get_config().rerank_weights
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
    except Exception:
        pass
    return _RERANK_WEIGHTS


_RERANK_TOKEN_RE = re.compile("\\b[A-Za-z][A-Za-z\\-_/]{2,}\\b")


_STRONG_BM25_THRESHOLD = 0.95  # bm25_score = 1/(1+exp(rank))

_CTR_WEIGHTS_CACHE: Optional[tuple[float, Any, bool]] = None
_CTR_WEIGHTS_TTL = 300  # 5 minutes


def _sp_lazy(name: str, default: object = None) -> object:
    """Read a lazy config flag from search_pipeline's __getattr__.

    The lazy config resolver lives in search_pipeline. This helper looks
    up the flag there, falling back to ``default`` if it's not set
    (e.g. during testing or before the config singleton is initialized).
    """
    sp = sys.modules.get("search_pipeline")
    if sp is None:
        return default
    return getattr(sp, name, default)


_RERANK_HALF_LIFE_DAYS = _sp_lazy("_RERANK_HALF_LIFE_DAYS", 180)


def _get_rerank_half_life_days() -> float:
    """Resolve rerank_half_life_days from config; falls back to 180.0."""
    try:
        from infra._lazy_imports import get_config

        return float(get_config().rerank_half_life_days)
    except Exception:
        return 180.0


def _reciprocal_rank_fusion(
    ranked_lists, k: int = _RRF_K, weights: Optional[list[float]] = None
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
    if _sp_lazy("_TEMPORAL_DECAY_MODE", "exponential") == "off":
        return 1.0
    if as_of is not None:
        now_ts = as_of
    if now_ts is None:
        now_ts = time.time()

    # Forgetting curve: decay based on last_accessed
    if _sp_lazy("_FORGETTING_CURVE_ENABLED", False) and last_accessed:
        try:
            la_ts = datetime.fromisoformat(last_accessed).timestamp()
            age_days = max(0.0, (now_ts - la_ts) / 86400.0)
            half_life = _sp_lazy("_FORGETTING_CURVE_HALF_LIFE", 30)
        except (ValueError, TypeError):
            # Fall through to created-based decay
            pass
        else:
            if _sp_lazy("_TEMPORAL_DECAY_MODE", "exponential") == "linear":
                return max(
                    0.0, 1.0 - float(age_days) / (3.0 * float(cast(float, half_life)))
                )
            return 0.5 ** (float(age_days) / float(cast(float, half_life)))  # type: ignore[no-any-return]

    # Standard decay based on created timestamp
    if not created:
        return 1.0
    try:
        c_ts = datetime.fromisoformat(created).timestamp()
        age_days = max(0.0, (now_ts - c_ts) / 86400.0)
    except (ValueError, TypeError):
        return 1.0
    if _sp_lazy("_TEMPORAL_DECAY_MODE", "exponential") == "linear":
        return max(
            0.0,
            1.0
            - float(age_days)
            / (3.0 * float(cast(float, _sp_lazy("_TEMPORAL_DECAY_HALF_LIFE", 180)))),
        )
    return 0.5 ** (  # type: ignore[no-any-return]
        float(age_days) / float(cast(float, _sp_lazy("_TEMPORAL_DECAY_HALF_LIFE", 180)))
    )


def _apply_neural_forget_curve(scored_results: list, query: str) -> list:
    """Apply neural-forget-curve surprise-based re-ranking.

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
        q_tokens = set(query.lower().split())
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
            last_tokens = set(last_q.lower().split())
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
            last_accessed = r[10] if len(r) > 10 else None
            adjusted = final_score * penalty
            new_r = list(r)
            if len(new_r) >= 7:
                new_r[6] = adjusted
            modified.append(tuple(new_r))
        except Exception:
            logger.warning("Failed to apply surprise penalty to result")
            modified.append(r)
    return modified


def _apply_temporal_decay(
    scored_results: list,
    decay_weight: float = 0.15,
    as_of: Optional[float] = None,
) -> list:
    """Apply temporal decay to scored results as a post-retrieval modifier.

    Sprint 5: ``as_of`` is forwarded to ``_temporal_decay_factor`` so
    recency is calculated relative to the time-travel anchor instead of
    ``time.time()``.

    Multiplies each result's final_score by (1 - decay_weight + decay_weight * decay_factor).
    This boosts recent notes and gently penalizes old ones without
    overriding the relevance-based ranking entirely.
    When MEMORY_FORGETTING_CURVE=1, uses last_accessed for Ebbinghaus-style decay.
    decay_weight is read from config (temporal_decay_weight) when not explicitly passed.
    """
    if decay_weight == 0.15:
        try:
            from infra._lazy_imports import get_config

            decay_weight = float(get_config().temporal_decay_weight)
        except Exception:
            pass
    if _sp_lazy("_TEMPORAL_DECAY_MODE", "exponential") == "off" or decay_weight <= 0:
        return scored_results
    now_ts = time.time() if as_of is None else as_of
    modified = []
    for r in scored_results:
        (
            note_id,
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
        last_accessed = r[10] if len(r) > 10 else None
        metadata_json = r[11] if len(r) > 11 else None
        decay = _temporal_decay_factor(
            created, now_ts, last_accessed=last_accessed, as_of=as_of
        )
        adjusted = final_score * (1.0 - decay_weight + decay_weight * decay)
        modified.append(
            (
                note_id,
                content,
                source_file,
                tags_json,
                created,
                rank,
                adjusted,
                fitness,
                importance,
                pinned,
                last_accessed,
                metadata_json,
            )
        )
    return modified


def _strong_match_float(rows):
    """Bucket ``rows`` so unambiguous FTS5 hits (bm25_score >= 0.95) come
    first relative to the rest of the list. Within each bucket, the
    original relative order is preserved.
    """
    if not rows:
        return rows
    strong, rest = [], []
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
        recency:    0.10  days-since-created, exp decay, half-life 180d
        tag_match:  0.05  fraction of query tokens present in tags

    QW3: pass ``weights`` to override per-channel weights (e.g. for
    query-type-adaptive scoring). The override must contain all six
    channel keys and should sum to 1.0; missing keys inherit from
    ``_RERANK_WEIGHTS``.

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
    recency_factor = 0.0
    if ctx.created:
        try:
            c_ts = datetime.fromisoformat(ctx.created).timestamp()
            age_days = max(0.0, (now_ts - c_ts) / 86400.0)
            recency_factor = 0.5 ** (age_days / _get_rerank_half_life_days())
        except (ValueError, TypeError):
            recency_factor = 0.0
    recency_factor *= ctx.recency_weight
    tag_match = 0.0
    query_tokens = {
        t.lower() for t in _RERANK_TOKEN_RE.findall(ctx.query) if len(t) >= 3
    }
    if query_tokens:
        try:
            tags_list = json.loads(ctx.tags_json) if ctx.tags_json else []
        except Exception:
            logger.warning("Failed to parse tags JSON for tag scoring")
            tags_list = []
        if tags_list:
            tag_tokens = {
                t.lower() for t in tags_list if isinstance(t, str) and len(t) >= 3
            }
            if tag_tokens:
                hits = len(query_tokens & tag_tokens)
                tag_match = min(1.0, hits / max(1, len(query_tokens)))
    return (  # type: ignore[no-any-return]
        weights.get("bm25", _get_rerank_weights()["bm25"]) * bm25_score
        + weights.get("fitness", _get_rerank_weights()["fitness"]) * fitness_score
        + weights.get("importance", _get_rerank_weights()["importance"])
        * importance_normalized
        + weights.get("pinned", _get_rerank_weights()["pinned"]) * pinned_bonus
        + weights.get("recency", _get_rerank_weights()["recency"]) * recency_factor
        + weights.get("tag_match", _get_rerank_weights()["tag_match"]) * tag_match
    )


def _apply_exploration(cached_stats) -> Optional[dict]:
    if cached_stats is None:
        return None
    alphas, betas, expected = cached_stats
    from config import get_config
    try:
        cfg = get_config()
        mode = os.environ.get("MEMORY_EXPLORATION_MODE", getattr(cfg, "exploration_mode", "off")).lower()
    except Exception:
        mode = os.environ.get("MEMORY_EXPLORATION_MODE", "off").lower()

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
    """Compute channel weights from CTR feedback data.

    Queries ``memory_ctr_feedback`` for recent click/dismiss signals (last 30
    days).  For each query group, parses the stored ``ranking_params`` JSON to
    recover the per-channel weights that were used, then computes a weighted
    average biased by query-level CTR (clicks / (clicks + dismissals)).

    Returns adjusted weights dict if ≥10 data points exist, otherwise ``None``
    (caller should fall back to ``_RERANK_WEIGHTS``).

    Gated behind ``MEMORY_CTR_TUNING=1`` env var.  Results are cached for
    ``_CTR_WEIGHTS_TTL`` seconds so at most one DB query runs per search.
    """
    # If the gating env var changed since the cache was populated, drop
    # the cache. Otherwise we'd return a stale result from before the
    # flag flipped. (Discovered during audit; testing tooling toggles
    # the env var between sessions.)
    env_now = os.environ.get("MEMORY_CTR_TUNING") == "1"
    global _CTR_WEIGHTS_CACHE

    if _CTR_WEIGHTS_CACHE is not None:
        ts, cached, cached_env = _CTR_WEIGHTS_CACHE
        if cached_env != env_now or time.time() - ts >= _CTR_WEIGHTS_TTL:
            _CTR_WEIGHTS_CACHE = None

    if _CTR_WEIGHTS_CACHE is not None:
        ts, cached, _cached_env = _CTR_WEIGHTS_CACHE
        return _apply_exploration(cached)

    if not env_now:
        _CTR_WEIGHTS_CACHE = (time.time(), None, env_now)
        return None

    try:
        from infra._lazy_imports import connection_pool

        db = connection_pool.get(str(db_path), timeout=5.0)
        try:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "memory_ctr_feedback" not in tables:
                _CTR_WEIGHTS_CACHE = (time.time(), None, env_now)
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
                _CTR_WEIGHTS_CACHE = (time.time(), None, env_now)
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
                _CTR_WEIGHTS_CACHE = (time.time(), None, env_now)
                return None

            adjusted = {ch: channel_sums[ch] / total_weight for ch in _RERANK_WEIGHTS}
            s = sum(adjusted.values())
            if s > 0:
                adjusted = {ch: v / s for ch, v in adjusted.items()}
            else:
                adjusted = _RERANK_WEIGHTS

            stats = (alphas, betas, adjusted)
            _CTR_WEIGHTS_CACHE = (time.time(), stats, env_now)
            return _apply_exploration(stats)
        finally:
            connection_pool.put(db)
    except Exception:
        logger.warning("Failed to compute CTR weights from feedback")
        _CTR_WEIGHTS_CACHE = (time.time(), None, env_now)
        return None


_W_INPUT_DIM = 6   # query embedding dimension
_W_HIDDEN = 8      # hidden state dimension
_W_TOTAL = _W_HIDDEN + 1 + _W_HIDDEN * _W_INPUT_DIM + 1  # 58


class TemporalAttentionModel:
    """Lightweight SSM-style temporal attention model for note recency scoring.

    Weights layout (58 elements total):
        W_readout[0:8]    — linear readout head
        b_readout[8]      — readout bias
        W_input[8:56]     — 8×6 input-weight matrix (row-major)
        b_input[56:57]    — input bias
    """

    def __init__(self, weights=None):
        self.has_learned_weights = False
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
        if not self.has_learned_weights or note_id not in self._hidden:
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
