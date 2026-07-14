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
import os
from abc import ABC, abstractmethod
from typing import cast

from search.config import get_search_config

import numpy as _np

# 2026-06-23: Removed top-level search_pipeline import to resolve circular import.
# _LATE_INTERACTION_ENABLED is resolved directly via get_config() to keep configuration clean.

logger = logging.getLogger(__name__)

_mps_warned = False

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

# CE chunk rerank cache: query_hash -> (timestamp, ce_scores_list)
import threading
_ce_score_cache: dict[str, tuple[float, list[float]]] = {}
_ce_cache_lock = threading.Lock()
_ce_model_load_lock = threading.Lock()
_CE_CACHE_TTL = 300  # 5 minutes
_CE_CACHE_MAX = 128  # max cached entries

# Late-interaction content tokenization and ngrams cache: memory_id -> (content_hash, content_tokens, content_ngrams)
_li_content_cache: dict[str, tuple[int, list[str], list[set[str]]]] = {}
_li_cache_lock = threading.Lock()
_LI_CACHE_MAX = 1000


def _get_cross_encoder_blend() -> float:
    return get_search_config().cross_encoder_blend


def _get_late_interaction_blend() -> float:
    return get_search_config().late_interaction_blend


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
                global _mps_warned
                if not _mps_warned:
                    logger.warning(
                        "deep_rerank requested but only MPS backend is available; "
                        "falling back to weak CE for this query."
                    )
                    _mps_warned = True
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
    # Precompute content tokens and ngrams for all candidates BEFORE the loop, utilizing cache
    content_tokens = []
    content_ngrams = []
    for r in head:
        mid = r[0]
        content = r[1] or ""
        h_val = hash(content)

        cached = None
        with _li_cache_lock:
            if mid in _li_content_cache:
                c_hash, c_toks, c_ngs = _li_content_cache[mid]
                if c_hash == h_val:
                    cached = (c_toks, c_ngs)

        if cached is not None:
            c_toks, c_ngs = cached
        else:
            c_toks = _tokenize_for_ce(content)
            c_ngs = []
            for tok in c_toks:
                if len(tok) >= 3:
                    c_ngs.append({tok[i : i + 3] for i in range(len(tok) - 2)})
                else:
                    c_ngs.append(set())
            with _li_cache_lock:
                if len(_li_content_cache) >= _LI_CACHE_MAX:
                    _li_content_cache.clear()
                _li_content_cache[mid] = (h_val, c_toks, c_ngs)

        content_tokens.append(c_toks)
        content_ngrams.append(c_ngs)
    reranked = []
    for i, r in enumerate(head):
        content = r[1]
        final_score = r[6]
        # Use pre-computed query ngrams AND pre-computed content ngrams
        if q_ngrams and content:
            c_tokens = content_tokens[i]
            c_ng = content_ngrams[i]
            li_score, li_avg_dist = _late_interaction_score_batch(q_ngrams, c_tokens, c_ng)
        else:
            li_score = 0.0
            li_avg_dist = float(len(content_tokens[i]))
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
_CE_CHUNK_MODEL_ERROR = None  # timestamp of last failed load attempt
_CE_CHUNK_RETRY_COOLDOWN = 60.0  # seconds before retrying after failure
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


def _get_best_device() -> str:
    """Determine the best hardware accelerator available for PyTorch."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def _get_ce_chunk_model():
    """Lazily load the cross-encoder model for chunk-level reranking.

    Uses ms-marco-MiniLM-L-6-v2 — fast (1.2ms/doc) and accurate for
    the current pipeline. bge-reranker-v2-m3 was tested but is 11x slower
    with no ranking improvement on the golden eval.

    Implements a retry/eviction mechanism: if loading fails, subsequent
    calls retry after ``_CE_CHUNK_RETRY_COOLDOWN`` seconds rather than
    staying broken permanently.
    """
    global _CE_CHUNK_MODEL, _CE_CHUNK_MODEL_ERROR
    if _CE_CHUNK_MODEL is not None:
        return _CE_CHUNK_MODEL
    with _ce_model_load_lock:
        if _CE_CHUNK_MODEL is not None:
            return _CE_CHUNK_MODEL
        # If previous load failed, check cooldown before retrying
        if _CE_CHUNK_MODEL_ERROR is not None:
            import time as _time_mod
            if _time_mod.time() - _CE_CHUNK_MODEL_ERROR < _CE_CHUNK_RETRY_COOLDOWN:
                return None
        try:
            from sentence_transformers import CrossEncoder
            device = _get_best_device()
            logger.debug("_get_ce_chunk_model: loading CrossEncoder on device=%r", device)
            _CE_CHUNK_MODEL = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2", max_length=512, device=device)
            _CE_CHUNK_MODEL_ERROR = None  # Clear error on success
            return _CE_CHUNK_MODEL
        except Exception as e:
            logger.warning("_get_ce_chunk_model: failed to load CE model: %s", e)
            import time as _time_mod2
            _CE_CHUNK_MODEL_ERROR = _time_mod2.time()
            return None


def _get_ce_score_cache() -> dict[str, tuple[float, list[float]]]:
    """Return the module-level CE score cache."""
    return _ce_score_cache


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

    # --- FAST PATH: skip CE chunk reranking for simple queries ---
    # Single-word queries, very short queries, or queries with only stopwords
    # don't benefit from chunk-level CE scoring — FTS already handles them well.
    query_words = [w for w in query.split() if w.lower() not in _CE_STOPWORDS]
    if len(query_words) <= 1:
        logger.debug("_apply_ce_chunk_rerank: fast-path skip (simple query: %r)", query)
        return list(scored_results)

    # --- FAST PATH: skip if few candidates (no benefit from reranking) ---
    if len(scored_results) <= 5:
        logger.debug("_apply_ce_chunk_rerank: fast-path skip (%d candidates)", len(scored_results))
        return list(scored_results)

    model = _get_ce_chunk_model()
    if model is None:
        logger.warning("_apply_ce_chunk_rerank: CE model unavailable, skipping")
        return scored_results

    # --- REDUCE CANDIDATES: only rerank top-50 (not top-300) ---
    # The CE model is the bottleneck (7s for 300 candidates). Limiting to 50
    # reduces latency ~6x while keeping the gold result in the rerank pool.
    effective_top_k = min(top_k, 50)
    head = scored_results[:effective_top_k]
    tail = scored_results[effective_top_k:]
    logger.debug("_apply_ce_chunk_rerank: scoring %d candidates (of %d total)", len(head), len(scored_results))

    # --- PRE-FILTER: skip CE for sessions with high FTS scores ---
    # If a session already has a strong FTS score (top 20%), it likely matches
    # well and doesn't need CE reranking. This cuts the CE candidate pool ~5x.
    fts_scores = [float(r[6]) if r[6] is not None else 0.0 for r in head]
    if fts_scores:
        p80 = _np.percentile(fts_scores, 80) if len(fts_scores) >= 5 else 0.0
    else:
        p80 = 0.0
    # Only rerank sessions below the 80th percentile (need CE help)
    ce_candidates = []
    ce_passthrough = []
    for i, r in enumerate(head):
        if fts_scores[i] >= p80 and p80 > 0.0:
            # High FTS score — pass through without CE
            ce_passthrough.append(r)
        else:
            ce_candidates.append(r)
    logger.debug("_apply_ce_chunk_rerank: %d need CE, %d pass through (p80=%.3f)",
                 len(ce_candidates), len(ce_passthrough), p80)

    # If few candidates need CE, skip the expensive scoring
    if len(ce_candidates) <= 2:
        logger.debug("_apply_ce_chunk_rerank: too few CE candidates, skipping")
        return list(scored_results)

    # --- CACHE: check CE score cache before scoring ---
    import hashlib
    _ce_cache = _get_ce_score_cache()
    candidate_ids = ",".join(str(r[0]) for r in ce_candidates)
    cache_key = hashlib.sha256(f"{query}:{candidate_ids}".encode()).hexdigest()[:16]

    with _ce_cache_lock:
        if cache_key in _ce_cache:
            cached_ts, cached_scores = _ce_cache[cache_key]
            import time as _time_check
            if _time_check.time() - cached_ts < _CE_CACHE_TTL:
                logger.debug("_apply_ce_chunk_rerank: cache hit for %s", cache_key)
                # Apply cached scores to ce_candidates
                ce_reranked = []
                for r, ce_score in zip(ce_candidates, cached_scores[:len(ce_candidates)]):
                    final_score = float(r[6]) if r[6] is not None else 0.0
                    ce_norm = max(0.0, min(1.0, (ce_score + 10.0) / 20.0))
                    adjusted = final_score * (1.0 - blend) + ce_norm * blend
                    new_r = list(r)
                    new_r[6] = adjusted
                    ce_reranked.append(tuple(new_r))
                # Merge ce_reranked + ce_passthrough + tail
                all_reranked = ce_reranked + ce_passthrough
                all_reranked.sort(key=lambda x: float(x[6]) if x[6] is not None else 0.0, reverse=True)
                return all_reranked + tail
            else:
                del _ce_cache[cache_key]

    # Build all (query, chunk) pairs across all sessions at once,
    # then batch-predict for ~10x speedup over per-session calls.
    import time as _t
    _t0 = _t.time()
    all_pairs = []
    chunk_counts = []  # how many chunks per session
    for r in ce_candidates:
        content = r[1] or ""
        chunks = _chunk_text(content)
        if len(chunks) > 2:
            # Filter chunks based on query word overlap to reduce CrossEncoder workload
            query_word_set = set(w.lower() for w in query.split() if w.lower() not in _CE_STOPWORDS)
            if not query_word_set:
                query_word_set = set(w.lower() for w in query.split())
            
            scored_chunks = []
            for chunk in chunks:
                chunk_words = set(chunk.lower().split())
                overlap = len(query_word_set.intersection(chunk_words))
                scored_chunks.append((overlap, chunk))
            
            # Sort descending by overlap, keep top 2
            scored_chunks.sort(key=lambda x: x[0], reverse=True)
            chunks = [c for _, c in scored_chunks[:2]]
        chunk_pairs = [(query, c[:512]) for c in chunks]
        all_pairs.extend(chunk_pairs)
        chunk_counts.append(len(chunk_pairs))
    logger.debug("_apply_ce_chunk_rerank: %d sessions, %d total chunks (%.2fms chunking)",
                 len(ce_candidates), len(all_pairs), (_t.time()-_t0)*1000)

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

    # --- CACHE: store CE scores for future lookups ---
    with _ce_cache_lock:
        # Evict old entries if cache is full
        if len(_ce_cache) >= _CE_CACHE_MAX:
            oldest_key = min(_ce_cache, key=lambda k: _ce_cache[k][0])
            del _ce_cache[oldest_key]
        _ce_cache[cache_key] = (_t.time(), ce_scores)

    # Blend CE score with existing final_score for ce_candidates
    ce_reranked = []
    for r, ce_score in zip(ce_candidates, ce_scores):
        final_score = float(r[6]) if r[6] is not None else 0.0
        # Normalize CE score to [0, 1] range (ms-marco scores are roughly [-10, 10])
        ce_norm = max(0.0, min(1.0, (ce_score + 10.0) / 20.0))
        adjusted = final_score * (1.0 - blend) + ce_norm * blend
        new_r = list(r)
        new_r[6] = adjusted
        ce_reranked.append(tuple(new_r))

    # Merge ce_reranked + ce_passthrough + tail
    all_reranked = ce_reranked + ce_passthrough
    all_reranked.sort(key=lambda x: float(x[6]) if x[6] is not None else 0.0, reverse=True)
    return all_reranked + tail


# ---------------------------------------------------------------------------
# PR1.2: Single Monotonic CE
# ---------------------------------------------------------------------------
# Historically two CE stages both rewrote r[6]: a "weak" single-doc CE
# (stage 9b) and a "chunk" CE (stage 9c). Whichever ran LAST owned the
# order -> non-deterministic / conflicting ranking. PR1.2 collapses them
# into ONE CE stage selected by query type, so ranking is deterministic
# and monotonic.


def _get_ce_blend() -> float:
    """PR1.2: blend constant for the weak / deep CE stages (plan default 0.85)."""
    return get_search_config().ce_blend


def _get_ce_chunk_blend() -> float:
    """PR1.2: blend for the chunk-level CE.

    Defaults to 0.7 -- the value the PR1.1 pipeline validated against
    (weak hand-rolled 0.6 + chunk 0.7). The single-CE chunk stage runs
    ALONE (no weak pre-step), so its blend must match the validated
    full-pipeline behaviour to avoid a recall/ndcg regression. weak/deep
    use _get_ce_blend() (0.85, per the SOTA plan).
    """
    return get_search_config().ce_chunk_blend


def _detect_ce_query_type(query: str) -> str:
    """PR1.2: route a query to a single CE stage.

    Returns ``"chunk"`` for long / multi-part / conversational queries
    (answers buried in long docs where chunk-level CE wins) and ``"weak"``
    for everything else (short keyword, factual, code).

    Conservative: when in doubt, pick ``"weak"`` (the cheap always-available
    single-doc CE). The classifier only escalates to ``"chunk"`` on clear
    long / multi-part signals.
    """
    if not query:
        return "weak"
    q = query.strip()
    # Code fences / declarations -> precise single-doc matching is best.
    if "```" in q or q.startswith("def ") or q.startswith("class ") or q.startswith("function "):
        return "weak"
    # A real question (ends with '?') is conversational -> chunk-level CE
    # catches answers buried deep in long multi-topic sessions.
    if "?" in q:
        return "chunk"
    # Multi-part signal: several clause boundaries (semicolons / newlines)
    # => conversational / multi-hop => chunk.
    parts = [part for part in re.split(r"[;\n]", q) if part.strip()]
    if len(parts) >= 2:
        return "chunk"
    # Long queries (>= 8 words) are rarely a single keyword; route to chunk
    # (the V2 winner for long docs).
    if len(q.split()) >= 8:
        return "chunk"
    return "weak"


def _select_ce_mode(query: str, deep_rerank_param: bool = False) -> str:
    """PR1.2: choose exactly one CE stage for this query.

    Precedence:
      * deep  -- if ``MEMORY_CE_DEEP=1`` (config ce_deep_enabled) OR the
                 legacy ``deep_rerank`` parameter is set. Applied to top-30
                 only; degrades gracefully to weak/chunk if the model is
                 unavailable (see _apply_single_ce_rerank).
      * chunk -- long / multi-part / conversational queries.
      * weak  -- default (short keyword / factual / code).
    """
    deep_enabled = False
    try:
        from infra._lazy_imports import get_config

        deep_enabled = bool(get_config().ce_deep_enabled)
    except Exception:  # pragma: no cover - best-effort config read
        deep_enabled = False
    if os.environ.get("MEMORY_CE_DEEP") == "1":
        deep_enabled = True
    if deep_enabled or deep_rerank_param:
        return "deep"
    # Non-deep path: the validated PR1.1 baseline combines BOTH the weak
    # (hand-rolled 0.6) and chunk (ms-marco 0.7) signals, so the single CE
    # stage must run the combined path to reproduce it exactly.
    # ``_detect_ce_query_type`` is retained for future query-type-aware
    # tuning but is intentionally NOT used for mode selection here.
    return "combined"


def _apply_weak_ce_rerank(
    query: str, scored_results: list, top_k: int, blend: float | None = None
) -> list:
    """PR1.2: single-doc weak CE.

    Uses the ms-marco-MiniLM cross-encoder (``cross-encoder/ms-marco-
    MiniLM-L-6-v2``, shared with the chunk CE) on the whole document when it
    is available locally; otherwise falls back to the hand-rolled IDF+bigram
    weak CE (``_cross_encoder_score``) -- the pre-PR1.2 default. Writes r[6]
    exactly once and re-sorts once.
    """
    if blend is None:
        blend = _get_ce_blend()
    if not scored_results or not query:
        return list(scored_results)
    try:
        from infra._lazy_imports import get_config

        if get_config().reranker_disabled:
            return list(scored_results)
    except Exception:  # pragma: no cover - best-effort config read
        pass
    head = scored_results[:top_k]
    tail = scored_results[top_k:]
    docs = [r[1] or "" for r in head]
    model = _get_ce_chunk_model()  # ms-marco-MiniLM-L-6-v2 (shared with chunk CE)
    if model is not None:
        try:
            pairs = [(query, (d or "")[:512]) for d in docs]
            raw = model.predict(pairs, show_progress_bar=False, batch_size=64)
            ce_scores = [float(x) for x in raw]
            ce_norm = [max(0.0, min(1.0, (s + 10.0) / 20.0)) for s in ce_scores]
        except Exception as e:  # pragma: no cover - model present but scored badly
            logger.debug("weak CE model scoring failed (%s); hand-rolled fallback", e)
            ce_norm = [_cross_encoder_score(query, d) for d in docs]
    else:
        ce_norm = [_cross_encoder_score(query, d) for d in docs]
    reranked = []
    for r, ce in zip(head, ce_norm):
        final_score = float(r[6]) if r[6] is not None else 0.0
        adjusted = final_score * (1.0 - blend + blend * ce)
        new_r = list(r)
        new_r[6] = adjusted
        reranked.append(tuple(new_r))
    reranked.sort(key=lambda x: float(x[6]) if x[6] is not None else 0.0, reverse=True)
    return reranked + tail


def _try_deep_rerank(query: str, scored_results: list, top_k: int = 30) -> list | None:
    """PR1.2: best-effort deep CE (Qwen3-Reranker-0.6B, BGE-m3 fallback).

    Returns the reranked list on success, or ``None`` if the model is
    unavailable / errors / is disabled -- the caller then falls back to the
    weak/chunk CE. NEVER raises. Applies to the top ``top_k`` (default 30)
    candidates only; the rest pass through untouched.
    """
    if not scored_results or not query:
        return None
    try:
        from infra._lazy_imports import get_config

        if get_config().reranker_disabled:
            return None
    except Exception:  # pragma: no cover - best-effort config read
        pass
    head = scored_results[:top_k]
    tail = scored_results[top_k:]
    blend = _get_ce_blend()
    try:
        from infra.reranker import get_reranker, normalize_rerank_score

        reranker = get_reranker()
        from infra._lazy_imports import get_config

        raw = reranker.score(
            query, [r[1] or "" for r in head], timeout=float(get_config().deep_rerank_timeout)
        )
        if raw is None:
            return None
        backend = reranker.backend()
        ce_scores = [normalize_rerank_score(s, backend=backend) for s in raw]
    except Exception as e:  # pragma: no cover - model unavailable
        logger.debug("deep rerank unavailable (%s); caller will fall back", e)
        return None
    reranked = []
    for r, ce in zip(head, ce_scores):
        final_score = float(r[6]) if r[6] is not None else 0.0
        adjusted = final_score * (1.0 - blend + blend * ce)
        new_r = list(r)
        new_r[6] = adjusted
        reranked.append(tuple(new_r))
    reranked.sort(key=lambda x: float(x[6]) if x[6] is not None else 0.0, reverse=True)
    return reranked + tail


def _assign_rank_once(scored_results: list, final_r6: list, chunk_k: int) -> list:
    """Assign ``r[6]`` EXACTLY ONCE per item, then sort the top ``chunk_k``
    by the new score and append the untouched tail in original order.

    This is the single write point for the combined CE stage: every
    candidate gets exactly one ``r[6]`` assignment, so there is no
    sequential-overwrite race between the weak and chunk signals.
    """
    out = []
    for i, r in enumerate(scored_results):
        new_r = list(r)
        new_r[6] = final_r6[i]
        out.append(tuple(new_r))
    head = out[:chunk_k]
    tail = out[chunk_k:]
    head.sort(key=lambda x: float(x[6]) if x[6] is not None else 0.0, reverse=True)
    return head + tail


def _apply_combined_ce_rerank(
    query: str, scored_results: list, weak_k: int, chunk_k: int
) -> list:
    """PR1.2 (option 2): ONE deterministic CE stage that reproduces the
    exact PR1.1 baseline (weak hand-rolled 0.6 pre-pass, then chunk
    ms-marco 0.7) with EXACTLY ONE ``r[6]`` write per item.

    Baseline ran two sequential stages, each mutating ``r[6]``:
      * stage 9b (weak):  r6 = raw * (1 - 0.6 + 0.6 * weak_ce)   [top limit*2]
      * stage 9c (chunk): r6 = r6 * (1 - 0.7) + chunk_ce_norm * 0.7
                          [top limit*3, with a p80 pre-filter: items
                           >= p80 keep their weak-adjusted r6 untouched]
    This function computes BOTH signals and assigns ``r[6]`` once, so the
    final score -- and therefore the ranking -- is identical to the
    sequential baseline but independent of stage execution order.

    The chunk sub-step mirrors ``_apply_ce_chunk_rerank`` exactly: the
    ms-marco model, the query-word-overlap top-2 chunk filter, the
    ``(s + 10) / 20`` normalization, the p80 pre-filter, and the simple-
    query / few-candidates / model-unavailable fast-paths. The only
    behavioural difference from the baseline is that the weak and chunk
    effects are combined into a single ``r[6]`` assignment instead of two.

    Returns the full list: top ``chunk_k`` sorted by final ``r[6]``, tail
    in original order (baseline's ``all_reranked + tail`` contract).
    """
    if not scored_results or not query:
        return list(scored_results)
    try:
        from infra._lazy_imports import get_config

        if get_config().reranker_disabled:
            return list(scored_results)
    except Exception:  # pragma: no cover - best-effort config read
        pass

    n = len(scored_results)
    weak_k = min(weak_k, n)
    chunk_k = min(chunk_k, n)

    weak_blend = _get_cross_encoder_blend()   # 0.6 (PR1.1 baseline)
    chunk_blend = _get_ce_chunk_blend()       # 0.7 (PR1.1 baseline)

    # Raw channel scores (what stage 9b saw as r[6] before any CE).
    raw = [float(r[6]) if r[6] is not None else 0.0 for r in scored_results]

    # ---- weak pre-pass: single-doc ms-marco CE when available, else
    # hand-rolled IDF+bigram. MULTIPLICATIVE (matches baseline stage 9b).
    # Using the model here (not just the hand-rolled score) is what the
    # validated PR1.1 baseline did -- the hand-rolled signal alone pulls
    # recall down, the model-based weak pre-pass lifts it back to baseline.
    w6 = list(raw)
    model_w = _get_ce_chunk_model()  # ms-marco-MiniLM-L-6-v2 (shared w/ chunk)
    ce_norm_w = None
    if model_w is not None:
        docs = [(query, (scored_results[i][1] or "")[:512]) for i in range(weak_k)]
        try:
            raw_w = model_w.predict(docs, show_progress_bar=False, batch_size=64)
            ce_norm_w = [max(0.0, min(1.0, (float(x) + 10.0) / 20.0)) for x in raw_w]
        except Exception as e:  # pragma: no cover - model present but failed
            logger.debug("combined weak CE model failed (%s); hand-rolled", e)
            ce_norm_w = None
    for i in range(weak_k):
        if ce_norm_w is not None:
            ce = ce_norm_w[i]
        else:
            ce = _cross_encoder_score(query, scored_results[i][1] or "")
        w6[i] = w6[i] * (1.0 - weak_blend + weak_blend * ce)

    # Final scores start at the weak-adjusted values; chunk only rewrites
    # the below-p80 candidates within the chunk window.
    final = list(w6)

    # Chunk CE fast-paths (mirror baseline _apply_ce_chunk_rerank).
    query_words = [w for w in query.split() if w.lower() not in _CE_STOPWORDS]
    model = _get_ce_chunk_model()
    if len(query_words) <= 1 or n <= 5 or model is None:
        return _assign_rank_once(scored_results, final, chunk_k)

    head_r6 = w6[:chunk_k]
    try:
        p80 = float(_np.percentile(head_r6, 80)) if len(head_r6) >= 5 else 0.0
    except Exception:
        p80 = 0.0
    ce_candidates = [
        i for i in range(chunk_k) if not (head_r6[i] >= p80 and p80 > 0.0)
    ]
    if len(ce_candidates) <= 2:
        return _assign_rank_once(scored_results, final, chunk_k)

    # Score the best chunk per candidate (top-2 chunks by query overlap).
    all_pairs: list = []
    chunk_counts: list = []
    idx_map: list = []
    for i in ce_candidates:
        content = scored_results[i][1] or ""
        chunks = _chunk_text(content)
        if len(chunks) > 2:
            qset = set(w.lower() for w in query.split() if w.lower() not in _CE_STOPWORDS)
            if not qset:
                qset = set(w.lower() for w in query.split())
            scored_chunks = []
            for c in chunks:
                cw = set(c.lower().split())
                scored_chunks.append((len(qset & cw), c))
            scored_chunks.sort(key=lambda x: x[0], reverse=True)
            chunks = [c for _, c in scored_chunks[:2]]
        pairs = [(query, c[:512]) for c in chunks]
        all_pairs.extend(pairs)
        chunk_counts.append(len(pairs))
        idx_map.append(i)

    if not all_pairs:
        return _assign_rank_once(scored_results, final, chunk_k)

    raw_scores = model.predict(all_pairs, show_progress_bar=False, batch_size=128)
    idx = 0
    ce_norm_map: dict = {}
    for j, i in enumerate(idx_map):
        sess = raw_scores[idx : idx + chunk_counts[j]]
        idx += chunk_counts[j]
        best = float(max(sess)) if len(sess) > 0 else 0.0
        ce_norm_map[i] = max(0.0, min(1.0, (best + 10.0) / 20.0))

    # SINGLE write: additive interpolation for below-p80 candidates only.
    for i in ce_candidates:
        cn = ce_norm_map.get(i, 0.0)
        final[i] = w6[i] * (1.0 - chunk_blend) + cn * chunk_blend

    return _assign_rank_once(scored_results, final, chunk_k)


class BaseReranker(ABC):
    """Strategy interface for a single cross-encoder rerank stage.

    Each concrete strategy implements :meth:`rerank`, which consumes the
    pre-CE ``scored_results`` (``r[6]`` = channel score) and returns a new
    list with ``r[6]`` rewritten exactly once. Selection by string ``mode``
    happens in :func:`get_reranker_strategy`, replacing the old inline
    ``if mode == "x"`` branching in ``_apply_single_ce_rerank``.
    """

    mode: str = "base"

    @abstractmethod
    def rerank(
        self,
        query: str,
        scored_results: list,
        top_k: int,
        weak_k: int,
        chunk_k: int,
    ) -> list:
        ...


class CombinedReranker(BaseReranker):
    """Default non-deep stage: ONE r[6] write reproducing the PR1.1 baseline."""

    mode = "combined"

    def rerank(self, query, scored_results, top_k, weak_k, chunk_k):
        return _apply_combined_ce_rerank(query, scored_results, weak_k, chunk_k)


class DeepReranker(BaseReranker):
    """Combined baseline + optional Qwen3-Reranker top-30 refinement.

    Degrades gracefully to the combined baseline when the deep model is
    unavailable or errors.
    """

    mode = "deep"

    def rerank(self, query, scored_results, top_k, weak_k, chunk_k):
        base = _apply_combined_ce_rerank(query, scored_results, weak_k, chunk_k)
        deep_out = _try_deep_rerank(query, base, top_k=min(len(base), 30))
        return deep_out if deep_out is not None else base


class ChunkReranker(BaseReranker):
    """Legacy standalone chunk CE (kept for back-compat)."""

    mode = "chunk"

    def rerank(self, query, scored_results, top_k, weak_k, chunk_k):
        return _apply_ce_chunk_rerank(
            query, scored_results, top_k=top_k, blend=_get_ce_chunk_blend()
        )


class WeakReranker(BaseReranker):
    """Legacy standalone weak CE (kept for back-compat)."""

    mode = "weak"

    def rerank(self, query, scored_results, top_k, weak_k, chunk_k):
        return _apply_weak_ce_rerank(
            query, scored_results, top_k=top_k, blend=_get_ce_blend()
        )


_RERANKER_REGISTRY: dict[str, BaseReranker] = {
    cls.mode: cls()
    for cls in (CombinedReranker, DeepReranker, ChunkReranker, WeakReranker)
}


def get_reranker_strategy(mode: str) -> BaseReranker:
    """Return the reranker strategy for ``mode``.

    Unknown modes fall back to :class:`WeakReranker` (the pre-PR1.2 default),
    preserving the prior ``if mode == ... else weak`` behaviour.
    """
    return _RERANKER_REGISTRY.get(mode, WeakReranker())


def _apply_single_ce_rerank(
    query: str,
    scored_results: list,
    top_k: int,
    mode: str,
    weak_k: int | None = None,
    chunk_k: int | None = None,
) -> list:
    """PR1.2 (option 2): the SINGLE monotonic CE stage.

    Modes:
      * ``"combined"`` -> ``_apply_combined_ce_rerank``: ONE function that
        computes BOTH the weak (hand-rolled 0.6) and chunk (ms-marco 0.7)
        signals and assigns ``r[6]`` exactly once. This reproduces the
        validated PR1.1 baseline and is the default non-deep path.
      * ``"deep"``     -> combined baseline, then an optional Qwen3-Reranker
         top-30 refinement (``_try_deep_rerank``). Graceful skip -> combined.
      * ``"chunk"``    -> legacy standalone chunk CE (kept for back-compat).
      * ``"weak"``     -> legacy standalone weak CE (kept for back-compat).

    The caller (orchestrator Phase 9) then applies late-interaction (a
    separate, non-CE reranker family) and re-asserts the PR1.1 rank-first
    sort. No CE stage runs after this point.
    """
    if not scored_results or not query:
        return list(scored_results)
    try:
        from infra._lazy_imports import get_config

        if get_config().reranker_disabled:
            return list(scored_results)
    except Exception:  # pragma: no cover - best-effort config read
        pass
    n = len(scored_results)
    wk = weak_k if weak_k is not None else min(n, top_k)
    ck = chunk_k if chunk_k is not None else min(n, top_k * 3 if mode == "deep" else top_k)
    return get_reranker_strategy(mode).rerank(query, scored_results, top_k, wk, ck)
