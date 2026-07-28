"""ColBERT MaxSim reranking for the search pipeline.

Implements the MaxSim scoring from ColBERT-v2:
    score(q, d) = Σ_i max_j sim(q_i, d_j)

where sim is cosine similarity between query and document token embeddings.
This replaces the 3-gram Jaccard proxy in search/rerankers.py that was
incorrectly labeled as "late interaction".

Adaptive depth: ColBERT only fires when (a) index is populated,
(b) candidates ≤ 30, (c) query tokens ≥ 3.  Cold-path stays with
weak CE.  Latency budget: 200 ms.
"""

from __future__ import annotations

import logging
import struct
import time
from typing import Any

logger = logging.getLogger(__name__)

_COLBERT_BLEND = 0.4  # Blend weight for ColBERT score in final ranking
_COLBERT_MAX_CANDIDATES = 30  # Only rerank when candidates ≤ this
_COLBERT_MIN_QUERY_TOKENS = 3  # Skip for short queries


def _blob_to_vec(blob: bytes) -> list[float]:
    """Unpack a float32 BLOB back to a list."""
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def _cosine_sim(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot: float = sum(x * y for x, y in zip(a, b))
    norm_a: float = sum(x * x for x in a) ** 0.5
    norm_b: float = sum(x * x for x in b) ** 0.5
    if norm_a < 1e-9 or norm_b < 1e-9:
        return 0.0
    return dot / (norm_a * norm_b)


def maxsim_score(query_vecs: list[list[float]], doc_vecs: list[list[float]]) -> float:
    """Compute ColBERT MaxSim score.

    For each query token, find the max cosine similarity with any doc token.
    Sum across all query tokens.
    """
    if not query_vecs or not doc_vecs:
        return 0.0
    total = 0.0
    for qv in query_vecs:
        best = -1.0
        for dv in doc_vecs:
            s = _cosine_sim(qv, dv)
            if s > best:
                best = s
        total += max(0.0, best)  # Clamp negative similarities
    return total


def colbert_rerank(
    conn: Any,
    query: str,
    candidates: list,  # list of result tuples (id, ..., final_score)
    db_path: Any = None,
    blend: float = _COLBERT_BLEND,
) -> list:
    """Rerank candidates using ColBERT MaxSim.

    Args:
        conn: Database connection with colbert_tokens populated.
        query: Original query string.
        candidates: List of 12-tuple result rows, sorted by final_score.
        db_path: Not used, kept for API compat with other rerankers.
        blend: Weight for ColBERT score in final blend.

    Returns:
        Re-ranked list with blended scores. If ColBERT is unavailable
        or conditions aren't met, returns candidates unchanged.
    """
    if not candidates or not query:
        return candidates

    # Adaptive depth gate: skip if too many candidates
    if len(candidates) > _COLBERT_MAX_CANDIDATES:
        logger.debug(
            "colbert_rerank: skip (%d candidates > %d limit)",
            len(candidates),
            _COLBERT_MAX_CANDIDATES,
        )
        return candidates

    # Check query token count (skip short queries)
    query_words = [w for w in query.split() if len(w) >= 2]
    if len(query_words) < _COLBERT_MIN_QUERY_TOKENS:
        logger.debug(
            "colbert_rerank: skip (%d query tokens < %d minimum)",
            len(query_words),
            _COLBERT_MIN_QUERY_TOKENS,
        )
        return candidates

    # Try to lazy-load the ColBERT model; skip gracefully if unavailable.
    from infra.colbert_encoder import _get_colbert_model
    _cm, _ct, _cp = _get_colbert_model()
    if _cm is None:
        logger.debug("colbert_rerank: skip (model not loaded)")
        return candidates

    # Check if index has data
    try:
        row = conn.execute("SELECT COUNT(*) FROM colbert_tokens LIMIT 1").fetchone()
        if not row or row[0] == 0:
            logger.debug("colbert_rerank: skip (colbert_tokens empty)")
            return candidates
    except Exception:
        return candidates

    # Encode query
    from infra.colbert_encoder import encode_query

    t0 = time.time()
    query_vecs = encode_query(query)
    if query_vecs is None:
        logger.debug("colbert_rerank: skip (encoder unavailable)")
        return candidates
    query_time_ms = (time.time() - t0) * 1000

    # Batch-fetch all ColBERT tokens in a single query (avoids N+1)
    _valid_items = [item for item in candidates if isinstance(item, (list, tuple)) and len(item) >= 7]
    _all_ids = [item[0] for item in _valid_items]
    _token_map: dict = {}
    if _all_ids:
        try:
            _placeholders = ",".join("?" * len(_all_ids))
            _rows = conn.execute(
                f"SELECT memory_id, vec FROM colbert_tokens WHERE memory_id IN ({_placeholders})",
                _all_ids,
            ).fetchall()
            from collections import defaultdict
            _grouped: dict = defaultdict(list)
            for _mid, _blob in _rows:
                _grouped[_mid].append(_blob_to_vec(_blob) if isinstance(_blob, bytes) else _blob)
            _token_map = dict(_grouped)
        except Exception as e:
            logger.debug("colbert_rerank: batch token fetch failed: %s", e)

    # Score each candidate
    results = []
    for item in candidates:
        if not isinstance(item, (list, tuple)) or len(item) < 7:
            results.append(item)
            continue
        memory_id = item[0]
        try:
            doc_vecs = _token_map.get(memory_id, [])
            if not doc_vecs:
                # No ColBERT tokens — keep original score
                results.append(item)
                continue
            raw_score = maxsim_score(query_vecs, doc_vecs)
            # Normalize to [0, 1] range (MaxSim is unbounded)
            norm_score = min(1.0, raw_score / max(1.0, len(query_vecs)))
            # Blend with original final_score
            original = item[6]
            blended = original * (1.0 - blend) + norm_score * blend
            # Create new tuple with updated score
            new_item = list(item)
            new_item[6] = blended
            results.append(tuple(new_item))
        except Exception as e:
            logger.debug("colbert_rerank: error scoring %s: %s", memory_id, e)
            results.append(item)

    total_ms = (time.time() - t0) * 1000
    logger.debug(
        "colbert_rerank: scored %d candidates in %.1f ms (query enc: %.1f ms)",
        len(candidates),
        total_ms,
        query_time_ms,
    )

    # Re-sort by updated final_score (item[6])
    results.sort(key=lambda r: r[6] if isinstance(r, (list, tuple)) and len(r) > 6 else 0, reverse=True)
    return results
