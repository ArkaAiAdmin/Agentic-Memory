"""Cross-encoder and late-interaction rerank primitives.

Extracted from search_pipeline.py (2026-06-20) as part of the god-module
decomposition. Contains:

- _tokenize_for_ce: shared CE tokenization (QW4)
- _cross_encoder_score: weak hand-rolled CE (IDF + bigram bonus)
- _apply_cross_encoder_rerank: CE rerank block (supports deep_rerank)
- _late_interaction_score: character-n-gram late-interaction proxy
- _precompute_query_ngrams: B15 fix — compute query ngrams once
- _late_interaction_score_batch: late-interaction using pre-computed ngrams
- _apply_late_interaction_rerank: late-interaction rerank block

Module-level config knobs ``_CROSS_ENCODER_BLEND`` and
``_LATE_INTERACTION_BLEND`` live here. ``_LATE_INTERACTION_ENABLED``
is resolved lazily through search_pipeline's module __getattr__ to
keep a single source of truth for config flags.

Behavior is identical to the inline versions. Re-exported from
search_pipeline for backward compat.
"""

from __future__ import annotations

import logging
import re

# 2026-06-23: Removed top-level search_pipeline import to resolve circular import.
# _LATE_INTERACTION_ENABLED is resolved directly via get_config() to keep configuration clean.

logger = logging.getLogger(__name__)

_CE_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "i",
        "you",
        "we",
        "they",
        "he",
        "she",
        "what",
        "which",
        "who",
        "whom",
        "how",
        "do",
        "does",
        "did",
        "can",
        "could",
        "would",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "will",
        "about",
        "if",
        "then",
        "so",
        "than",
        "but",
        "not",
        "no",
        "any",
        "some",
        "all",
        "my",
        "your",
        "our",
        "their",
        "his",
        "her",
    }
)
_CROSS_ENCODER_BLEND = 0.6
_LATE_INTERACTION_BLEND = 0.3


def _get_cross_encoder_blend() -> float:
    try:
        from _lazy_imports import get_config

        v = get_config().cross_encoder_blend
        return float(v)
    except Exception:
        return _CROSS_ENCODER_BLEND


def _get_late_interaction_blend() -> float:
    try:
        from _lazy_imports import get_config

        return get_config().late_interaction_blend
    except Exception:
        return _LATE_INTERACTION_BLEND


def _tokenize_for_ce(text: str) -> list:
    """QW4: lowercase + split on non-word chars, keep short tokens.

    Returns a list of tokens in original order. Duplicates are preserved
    so phrase detection still works; downstream IDF math uses a set.
    """
    if not text:
        return []
    return [
        t
        for t in re.findall("[\\w@\\#\\.\\+\\-]+", text.lower(), flags=re.UNICODE)
        if t
    ]


def _cross_encoder_score(query: str, content: str) -> float:
    """QW4: weak cross-encoder via IDF-weighted query coverage + phrase bonus.

    Returns a score in roughly [0, 1.2]. A score of 0 means the doc
    shares no meaningful tokens with the query. A score of 1.0 means
    the doc covers (almost) all of the query's weighted mass. Scores
    above 1.0 only happen when a query bigram is also found in the doc
    (phrase match bonus).
    """
    if not query or not content:
        return 0.0
    q_tokens = _tokenize_for_ce(query)
    c_tokens = _tokenize_for_ce(content)
    if not q_tokens or not c_tokens:
        return 0.0
    q_freq: dict = {}
    for t in q_tokens:
        q_freq[t] = q_freq.get(t, 0) + 1
    c_set = set(c_tokens)
    weighted_total = 0.0
    weighted_matched = 0.0
    for tok, freq in q_freq.items():
        w = 1.0 / (1.0 + (freq - 1) * 0.5)
        if tok in _CE_STOPWORDS and len(tok) <= 3:
            w *= 0.1
        weighted_total += w
        if tok in c_set:
            weighted_matched += w
    if weighted_total <= 0:
        return 0.0
    coverage = weighted_matched / weighted_total
    bonus = 0.0
    if len(q_tokens) >= 2 and len(c_tokens) >= 2:
        q_bigrams = [(q_tokens[i], q_tokens[i + 1]) for i in range(len(q_tokens) - 1)]
        c_bigrams = {(c_tokens[i], c_tokens[i + 1]) for i in range(len(c_tokens) - 1)}
        matched = sum((1 for bg in q_bigrams if bg in c_bigrams))
        if q_bigrams:
            bonus = min(0.2, 0.2 * matched / len(q_bigrams))
    return min(1.2, coverage + bonus)


def _apply_cross_encoder_rerank(
    query: str, scored_results: list, top_k: int, deep_rerank: bool = False
) -> list:
    """QW4: rerank the top `top_k` results using the cross-encoder, then
    return the full list (rest untouched). Each result is a 10-tuple
    matching the shape produced by the rerank block in search_memories:
        (id, content, source_file, tags, created, rank, final_score,
         fitness, importance, pinned)
    The cross-encoder multiplies final_score by
    (1 - blend) + blend * ce_score, so a doc with ce=0 still keeps
    50% of its channel-based score.

    deep_rerank=False (default): use the lightweight hand-rolled weak CE
    (IDF + bigram). Sub-millisecond, no extra deps.
    deep_rerank=True: use the Qwen3-Reranker-0.6B primary (or BAAI/bge-
    reranker-v2-m3 fallback) via the Reranker singleton. Both are MPS-safe
    and Apache 2.0 / MIT licensed. On any load/score failure, falls back
    to the weak CE so a missing model never breaks a search.
    """
    if not scored_results or not query:
        return scored_results
    try:
        from _lazy_imports import get_config
        if get_config().reranker_disabled:
            return list(scored_results)
    except Exception:
        pass
    head = scored_results[:top_k]
    tail = scored_results[top_k:]
    blend = _get_cross_encoder_blend()
    docs = [r[1] or "" for r in head]
    ce_scores: list = []
    if deep_rerank:
        try:
            import torch
            if torch.backends.mps.is_available() and not torch.cuda.is_available():
                logger.warning(
                    "deep_rerank requested but only MPS backend is available; "
                    "falling back to weak CE for this query."
                )
        except ImportError:
            pass
        try:
            from _lazy_imports import get_config
            from reranker import get_reranker, normalize_rerank_score

            reranker = get_reranker()
            raw = reranker.score(
                query, docs, timeout=float(get_config().deep_rerank_timeout)
            )
            if raw is not None:
                backend = reranker.backend()
                ce_scores = [normalize_rerank_score(s, backend=backend) for s in raw]
            else:
                logger.debug(
                    "reranker: model unavailable, using weak CE for this query"
                )
        except Exception as e:
            logger.debug("reranker: import/call failed (%s); using weak CE", e)
            ce_scores = []
    if not ce_scores:
        ce_scores = [_cross_encoder_score(query, d) for d in docs]
    reranked = []
    for r, ce in zip(head, ce_scores):
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
        adjusted = final_score * (1.0 - blend + blend * ce)
        reranked.append(
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
            )
        )
    reranked.sort(key=lambda x: x[6], reverse=True)
    return reranked + tail


def _late_interaction_score(query: str, content: str) -> float:
    """Compute a late-interaction similarity score between query and content.

    For each query token, find the best-matching document token (by
    character overlap + positional proximity) and accumulate the score.
    Returns a score in [0, 1].

    This is a lightweight approximation of real late interaction (e.g.
    ColBERT) that doesn't require loading a neural model. It uses
    character n-gram overlap as a proxy for token similarity and
    positional distance as a proxy for attention.
    """
    if not query or not content:
        return 0.0
    q_tokens = _tokenize_for_ce(query)
    c_tokens = _tokenize_for_ce(content)
    if not q_tokens or not c_tokens:
        return 0.0
    c_ngrams: list = []
    for tok in c_tokens:
        if len(tok) < 3:
            c_ngrams.append(set())
        else:
            c_ngrams.append({tok[i : i + 3] for i in range(len(tok) - 2)})
    total_score = 0.0
    max_possible = 0.0
    for qi, q_tok in enumerate(q_tokens):
        if len(q_tok) < 3:
            continue
        q_ngrams = {q_tok[i : i + 3] for i in range(len(q_tok) - 2)}
        max_possible += 1.0
        best_sim = 0.0
        best_dist = len(c_tokens)
        for ci, c_ng in enumerate(c_ngrams):
            if not c_ng or not q_ngrams:
                continue
            inter = len(q_ngrams & c_ng)
            union = len(q_ngrams | c_ng)
            sim = inter / union if union > 0 else 0.0
            dist = abs(qi - ci)
            proximity = 1.0 / (1.0 + dist * 0.1)
            weighted = sim * proximity
            if weighted > best_sim:
                best_sim = weighted
                best_dist = dist
        total_score += best_sim
    if max_possible <= 0:
        return 0.0
    return min(1.0, total_score / max_possible)


def _precompute_query_ngrams(query: str) -> list:
    """Pre-compute query 3-grams for batch late-interaction scoring.

    B15 fix: when _late_interaction_score is called once per top_k result,
    the query ngrams are recomputed every time.  This helper computes them
    once so _late_interaction_score_batch can reuse them.
    """
    if not query:
        return []
    q_tokens = _tokenize_for_ce(query)
    out = []
    for tok in q_tokens:
        if len(tok) >= 3:
            out.append({tok[i : i + 3] for i in range(len(tok) - 2)})
        else:
            out.append(set())
    return out


def _late_interaction_score_batch(
    query_ngrams: list, c_tokens: list, c_ngrams: list
) -> float:
    """Late-interaction score using pre-computed query ngrams.

    B15 fix: removes the per-call query ngram recomputation.
    """
    if not query_ngrams or not c_tokens or not c_ngrams:
        return 0.0
    total_score = 0.0
    max_possible = 0.0
    for qi, q_ngrams in enumerate(query_ngrams):
        if not q_ngrams:
            continue
        max_possible += 1.0
        best_sim = 0.0
        for ci, c_ng in enumerate(c_ngrams):
            if not c_ng or not q_ngrams:
                continue
            inter = len(q_ngrams & c_ng)
            union = len(q_ngrams | c_ng)
            sim = inter / union if union > 0 else 0.0
            dist = abs(qi - ci)
            proximity = 1.0 / (1.0 + dist * 0.1)
            weighted = sim * proximity
            if weighted > best_sim:
                best_sim = weighted
        total_score += best_sim
    if max_possible <= 0:
        return 0.0
    return min(1.0, total_score / max_possible)


def _apply_late_interaction_rerank(
    query: str, scored_results: list, top_k: int
) -> list:
    """Apply late interaction reranking to the top_k results.

    Blends the late interaction score with the existing final_score
    using _LATE_INTERACTION_BLEND. Returns the full list with adjusted
    scores for the top_k, rest untouched.
    """
    import sys

    # 2026-06-23: Query the config singleton directly to avoid import cycles.
    from _lazy_imports import get_config

    if not get_config().late_interaction or not scored_results or (not query):
        return scored_results
    head = scored_results[:top_k]
    tail = scored_results[top_k:]
    blend = _get_late_interaction_blend()
    # B15 fix: pre-compute query ngrams once instead of recomputing per result
    q_ngrams = _precompute_query_ngrams(query)
    reranked = []
    for r in head:
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
        # Use pre-computed query ngrams to avoid O(n²) recomputation
        if q_ngrams and content:
            c_tokens = _tokenize_for_ce(content or "")
            c_ngrams: list = []
            for tok in c_tokens:
                if len(tok) >= 3:
                    c_ngrams.append({tok[i : i + 3] for i in range(len(tok) - 2)})
                else:
                    c_ngrams.append(set())
            li_score = _late_interaction_score_batch(q_ngrams, c_tokens, c_ngrams)
        else:
            li_score = 0.0
        adjusted = final_score * (1.0 - blend) + li_score * blend
        reranked.append(
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
            )
        )
    reranked.sort(key=lambda x: x[6], reverse=True)
    return reranked + tail
