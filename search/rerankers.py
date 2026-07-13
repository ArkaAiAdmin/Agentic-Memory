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
from typing import cast

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
        from infra._lazy_imports import get_config

        v = get_config().cross_encoder_blend
        return float(v)
    except Exception as e:
        logger.warning("_get_cross_encoder_blend failed: %s", e)
        return _CROSS_ENCODER_BLEND


def _get_late_interaction_blend() -> float:
    try:
        from infra._lazy_imports import get_config

        return cast(float, get_config().late_interaction_blend)
    except Exception as e:
        logger.warning("_get_late_interaction_blend failed: %s", e)
        return _LATE_INTERACTION_BLEND


def _tokenize_for_ce(text: str | None) -> list:
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
    return the full list (rest untouched). Each result is a 12-tuple
    matching the shape produced by the rerank block in search_memories:
        (id, content, source_file, tags, created, rank, final_score,
         fitness, importance, pinned, last_accessed, avg_dist)
    The cross-encoder multiplies final_score by
    (1 - blend) + blend * ce_score, so a doc with ce=0 still keeps
    50% of its channel-based score. avg_dist is None (CE does not
    produce a positional-coherence signal).

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
        from infra._lazy_imports import get_config
        if get_config().reranker_disabled:
            return list(scored_results)
    except Exception as e:
        logger.warning("_apply_cross_encoder_rerank failed: %s", e)
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
            from infra._lazy_imports import get_config
            from infra.reranker import get_reranker, normalize_rerank_score

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
        final_score = r[6]
        adjusted = final_score * (1.0 - blend + blend * ce)
        new_r = list(r)
        new_r[6] = adjusted
        reranked.append(tuple(new_r))
    reranked.sort(key=lambda x: x[6], reverse=True)
    return reranked + tail


def _late_interaction_score(query: str, content: str) -> tuple[float, float]:
    """Compute a late-interaction similarity score between query and content.

    For each query token, find the best-matching document token (by
    character overlap + positional proximity) and accumulate the score.
    Returns ``(score, avg_best_dist)`` where score is in [0, 1] and
    avg_best_dist is the mean positional distance (in tokens) of each
    query token's best match — a proxy for topical coherence.

    This is a lightweight approximation of real late interaction (e.g.
    ColBERT) that doesn't require loading a neural model. It uses
    character n-gram overlap as a proxy for token similarity and
    positional distance as a proxy for attention.
    """
    if not query or not content:
        return 0.0, len(_tokenize_for_ce(content))
    q_tokens = _tokenize_for_ce(query)
    c_tokens = _tokenize_for_ce(content)
    if not q_tokens or not c_tokens:
        return 0.0, len(c_tokens)
    c_ngrams: list = []
    for tok in c_tokens:
        if len(tok) < 3:
            c_ngrams.append(set())
        else:
            c_ngrams.append({tok[i : i + 3] for i in range(len(tok) - 2)})
    total_score = 0.0
    total_dist = 0
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
        total_dist += best_dist
    if max_possible <= 0:
        return 0.0, float(len(c_tokens))
    avg_dist = total_dist / max_possible
    return min(1.0, total_score / max_possible), avg_dist


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
) -> tuple[float, float]:
    """Late-interaction score using pre-computed query ngrams.

    B15 fix: removes the per-call query ngram recomputation.
    Returns ``(score, avg_best_dist)`` for callers that want the
    positional-coherence signal alongside the similarity score.
    """
    if not query_ngrams or not c_tokens or not c_ngrams:
        return 0.0, float(len(c_tokens))
    total_score = 0.0
    total_dist = 0
    max_possible = 0.0
    for qi, q_ngrams in enumerate(query_ngrams):
        if not q_ngrams:
            continue
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
        total_dist += best_dist
    if max_possible <= 0:
        return 0.0, float(len(c_tokens))
    avg_dist = total_dist / max_possible
    return min(1.0, total_score / max_possible), avg_dist


def _apply_late_interaction_rerank(
    query: str, scored_results: list, top_k: int
) -> list:
    """Apply late interaction reranking to the top_k results.

    Blends the late interaction score with the existing final_score
    using _LATE_INTERACTION_BLEND. Returns the full list with adjusted
    scores for the top_k, rest untouched.  Each result is a 12-tuple:
        (id, content, source_file, tags, created, rank, final_score,
         fitness, importance, pinned, last_accessed, avg_dist)
    avg_dist [float | None] is the mean token-position distance of
    each query token's best match in the document — a proxy for
    topical coherence (lower = tighter co-occurrence).
    """

    # 2026-06-23: Query the config singleton directly to avoid import cycles.
    from infra._lazy_imports import get_config

    if not get_config().late_interaction or not scored_results or (not query):
        return scored_results
    head = scored_results[:top_k]
    tail = scored_results[top_k:]
    blend = _get_late_interaction_blend()
    # B15 fix: pre-compute query ngrams once instead of recomputing per result
    q_ngrams = _precompute_query_ngrams(query)
    reranked = []
    for r in head:
        content = r[1]
        final_score = r[6]
        # Use pre-computed query ngrams to avoid O(n²) recomputation
        if q_ngrams and content:
            c_tokens = _tokenize_for_ce(content or "")
            c_ngrams: list = []
            for tok in c_tokens:
                if len(tok) >= 3:
                    c_ngrams.append({tok[i : i + 3] for i in range(len(tok) - 2)})
                else:
                    c_ngrams.append(set())
            li_score, li_avg_dist = _late_interaction_score_batch(q_ngrams, c_tokens, c_ngrams)
        else:
            li_score = 0.0
            li_avg_dist = float(len(_tokenize_for_ce(content or "")))
        adjusted = final_score * (1.0 - blend) + li_score * blend
        new_r = list(r)
        new_r[6] = adjusted
        if len(new_r) > 13:
            new_r[13] = li_avg_dist
        reranked.append(tuple(new_r))
    reranked.sort(key=lambda x: x[6], reverse=True)
    return reranked + tail


# ---------------------------------------------------------------------------
# Chunk-level cross-encoder reranking (ms-marco-MiniLM-L-6-v2)
# ---------------------------------------------------------------------------

_CE_CHUNK_MODEL = None
_CE_CHUNK_SIZE = 150
_CE_CHUNK_OVERLAP = 30


def _chunk_text(text: str, chunk_size: int = _CE_CHUNK_SIZE, overlap: int = _CE_CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping word chunks for CE scoring."""
    if not text:
        return [""]
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunks.append(" ".join(words[i : i + chunk_size]))
        i += chunk_size - overlap
    return chunks if chunks else [text]


def _get_ce_chunk_model():
    """Lazily load the cross-encoder model for chunk-level reranking."""
    global _CE_CHUNK_MODEL
    if _CE_CHUNK_MODEL is not None:
        return _CE_CHUNK_MODEL
    try:
        from sentence_transformers import CrossEncoder
        _CE_CHUNK_MODEL = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2", max_length=512)
        return _CE_CHUNK_MODEL
    except Exception as e:
        logger.warning("_get_ce_chunk_model: failed to load CE model: %s", e)
        return None


def _apply_ce_chunk_rerank(
    query: str,
    scored_results: list,
    top_k: int = 300,
    blend: float = 0.7,
) -> list:
    """Chunk-level cross-encoder reranking using ms-marco-MiniLM-L-6-v2.

    For each result, splits the content into overlapping word chunks,
    scores each chunk against the query using the CE model, and uses
    the best chunk score as the session score. Results are re-ranked
    by blending the CE score with the existing final_score.

    This catches answers buried deep in long multi-topic conversations
    where the weak IDF+bigram CE fails.

    Args:
        query: Natural-language search query.
        scored_results: List of 12-tuple result rows from prior phases.
        top_k: Max results to rerank (rest pass through untouched).
        blend: Weight for CE score in the final blend (0.0-1.0).
            Higher = more CE influence. Default 0.7 (CE-dominated).

    Returns:
        Re-ranked list with CE-adjusted final_scores.
    """
    if not scored_results or not query:
        return scored_results
    try:
        from infra._lazy_imports import get_config
        if get_config().reranker_disabled:
            return list(scored_results)
    except Exception:
        pass

    model = _get_ce_chunk_model()
    if model is None:
        logger.warning("_apply_ce_chunk_rerank: CE model unavailable, skipping")
        return scored_results

    head = scored_results[:top_k]
    tail = scored_results[top_k:]
    logger.debug("_apply_ce_chunk_rerank: scoring %d candidates", len(head))

    # Build all (query, chunk) pairs across all sessions at once,
    # then batch-predict for ~10x speedup over per-session calls.
    import time as _t
    _t0 = _t.time()
    all_pairs = []
    chunk_counts = []  # how many chunks per session
    for r in head:
        content = r[1] or ""
        chunks = _chunk_text(content)
        chunk_pairs = [(query, c[:512]) for c in chunks]
        all_pairs.extend(chunk_pairs)
        chunk_counts.append(len(chunk_pairs))
    logger.debug("_apply_ce_chunk_rerank: %d sessions, %d total chunks (%.2fms chunking)",
                 len(head), len(all_pairs), (_t.time()-_t0)*1000)

    # Single batched prediction
    _t1 = _t.time()
    all_scores = model.predict(all_pairs, show_progress_bar=False, batch_size=128)
    logger.debug("_apply_ce_chunk_rerank: batch predict %.2fs", _t.time()-_t1)

    # Extract best chunk score per session
    ce_scores = []
    idx = 0
    for count in chunk_counts:
        session_scores = all_scores[idx : idx + count]
        if hasattr(session_scores, '__len__') and len(session_scores) > 0:
            best = session_scores[0]
            for s in session_scores[1:]:
                if s > best:
                    best = s
            ce_scores.append(float(best))
        else:
            ce_scores.append(0.0)
        idx += count

    # Blend CE score with existing final_score
    reranked = []
    for r, ce_score in zip(head, ce_scores):
        final_score = float(r[6]) if r[6] is not None else 0.0
        # Normalize CE score to [0, 1] range (ms-marco scores are roughly [-10, 10])
        ce_norm = max(0.0, min(1.0, (ce_score + 10.0) / 20.0))
        adjusted = final_score * (1.0 - blend) + ce_norm * blend
        new_r = list(r)
        new_r[6] = adjusted
        reranked.append(tuple(new_r))

    reranked.sort(key=lambda x: float(x[6]) if x[6] is not None else 0.0, reverse=True)
    return reranked + tail
