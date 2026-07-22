"""Answer-level reranking for the search pipeline.

Phase 5: For each candidate, extract a 100–300 token snippet that best
answers the query, then score the snippet-query relevance using the
existing cross-encoder (ms-marco-MiniLM).  This catches cases where a
relevant answer is buried deep in a long document — the main pipeline
scores the whole document, but answer-rerank scores the best snippet.

Pre-computation: hot memo IDs can be pre-scored via cron to keep online
latency tight.  The pre-computed scores are stored in a temp table and
looked up at query time.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ANSWER_RERANK_BLEND = 0.3  # Weight for answer score in final blend
_SNIPPET_MAX_TOKENS = 300
_SNIPPET_MIN_TOKENS = 50
_PRECOMPUTE_TABLE = "answer_rerank_cache"


def _ensure_cache_schema(conn: Any) -> None:
    """Create answer_rerank_cache table if it doesn't exist."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_PRECOMPUTE_TABLE} (
            memory_id   TEXT NOT NULL,
            query_hash  TEXT NOT NULL,
            score       REAL NOT NULL,
            snippet     TEXT NOT NULL,
            created_at  REAL NOT NULL DEFAULT (unixepoch()),
            UNIQUE (memory_id, query_hash)
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_arc_memory ON {_PRECOMPUTE_TABLE}(memory_id)"
    )


def _extract_snippet(content: str, query: str, max_tokens: int = _SNIPPET_MAX_TOKENS) -> str:
    """Extract the most relevant snippet from content for the query.

    Uses a simple keyword-overlap scoring to find the best window.
    Returns a snippet of ~max_tokens words centered on the best match.
    """
    if not content or not query:
        return content[:max_tokens * 5] if content else ""

    query_words = set(w.lower() for w in query.split() if len(w) >= 3)
    if not query_words:
        # No meaningful query words — return first N tokens
        words = content.split()
        return " ".join(words[:max_tokens])

    words = content.split()
    if len(words) <= max_tokens:
        return content

    # Sliding window: score each window by query word overlap
    best_score = -1
    best_start = 0
    window_size = max_tokens
    step = max(1, window_size // 4)  # 25% overlap

    for start in range(0, len(words) - window_size + 1, step):
        window = words[start : start + window_size]
        window_set = set(w.lower() for w in window if len(w) >= 3)
        score = len(query_words & window_set)
        if score > best_score:
            best_score = score
            best_start = start

    # Expand slightly around best window for context
    expand = window_size // 4
    start = max(0, best_start - expand)
    end = min(len(words), best_start + window_size + expand)
    return " ".join(words[start:end])


def _score_snippet(
    query: str, snippet: str, model: Any = None
) -> float:
    """Score query-snippet relevance using cross-encoder.

    Returns a score in [0, 1].  Uses the existing ms-marco-MiniLM
    cross-encoder if available, otherwise falls back to keyword overlap.
    """
    if not query or not snippet:
        return 0.0

    if model is not None:
        try:
            raw = model.predict(
                [(query, snippet)], show_progress_bar=False, batch_size=1
            )
            # Normalize from [-10, 10] to [0, 1]
            return max(0.0, min(1.0, (float(raw[0]) + 10.0) / 20.0))
        except Exception as e:
            logger.debug("CE score failed, falling back to keyword: %s", e)

    # Fallback: keyword overlap score
    query_words = set(w.lower() for w in query.split() if len(w) >= 3)
    snippet_words = set(w.lower() for w in snippet.split() if len(w) >= 3)
    if not query_words or not snippet_words:
        return 0.0
    overlap = len(query_words & snippet_words)
    return min(1.0, overlap / max(1, len(query_words)))


def answer_rerank(
    conn: Any,
    query: str,
    candidates: list,
    db_path: Any = None,
    blend: float = _ANSWER_RERANK_BLEND,
    model: Any = None,
    display_scores: Optional[dict] = None,
) -> list:
    """Rerank candidates by answer-level relevance.

    For each candidate:
    1. Extract the best snippet for the query
    2. Score snippet-query relevance via cross-encoder
    3. Blend answer score with original final_score

    Args:
        conn: Database connection.
        query: Original query string.
        candidates: List of 12-tuple result rows, sorted by final_score.
        db_path: Not used, kept for API compat.
        blend: Weight for answer score in final blend.
        model: Optional cross-encoder model instance.
        display_scores: Optional mapping ``{note_id: display_score}`` from
            the post-rank enrichment envelope (search.enrichment). When
            provided, the answer-level blend starts from the *enriched*
            score (concept / centrality / neural-forget surprise already
            folded in) instead of the raw final_score. This lets KG
            centrality and concept boosts influence the answer-level
            ordering without relaxing the RANK-FIRST LOCK (final_score
            itself is untouched). Recency is intentionally already inside
            final_score, so display_score excludes it (no double-count).

    Returns:
        Re-ranked list with blended scores.
    """
    if not candidates or not query:
        return candidates

    # Check pre-computed cache first
    _ensure_cache_schema(conn)
    query_hash = hashlib.sha256(query.encode()).hexdigest()[:16]

    results = []
    for item in candidates:
        if not isinstance(item, (list, tuple)) or len(item) < 7:
            results.append(item)
            continue

        memory_id = item[0]
        content = item[1] or ""
        # Honor the enriched baseline when available; fall back to raw r[6].
        if display_scores and memory_id in display_scores:
            try:
                original_score = float(display_scores[memory_id])
            except (TypeError, ValueError):
                original_score = item[6]
        else:
            original_score = item[6]

        # Check cache
        cached = conn.execute(
            f"SELECT score, snippet FROM {_PRECOMPUTE_TABLE} "
            "WHERE memory_id = ? AND query_hash = ?",
            (memory_id, query_hash),
        ).fetchone()

        if cached:
            answer_score = cached[0]
            snippet = cached[1]
        else:
            # Extract snippet and score
            snippet = _extract_snippet(content, query)
            answer_score = _score_snippet(query, snippet, model=model)

            # Cache the result
            try:
                conn.execute(
                    f"INSERT OR REPLACE INTO {_PRECOMPUTE_TABLE} "
                    "(memory_id, query_hash, score, snippet, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (memory_id, query_hash, answer_score, snippet, time.time()),
                )
            except Exception:
                pass  # Best-effort cache

        # Blend with original score
        blended = original_score * (1.0 - blend) + answer_score * blend
        new_item = list(item)
        new_item[6] = blended
        results.append(tuple(new_item))

    # Re-sort by updated final_score
    results.sort(
        key=lambda r: r[6] if isinstance(r, (list, tuple)) and len(r) > 6 else 0,
        reverse=True,
    )
    return results


def precompute_for_memory(
    conn: Any,
    memory_id: str,
    content: str,
    queries: list[str],
    model: Any = None,
) -> int:
    """Pre-compute answer rerank scores for a memory against multiple queries.

    Called by cron to keep hot memo IDs pre-scored.  Returns the number
    of query-memo pairs scored.
    """
    if not content or not queries:
        return 0

    _ensure_cache_schema(conn)
    scored = 0
    now = time.time()

    for query in queries:
        query_hash = hashlib.sha256(query.encode()).hexdigest()[:16]
        snippet = _extract_snippet(content, query)
        answer_score = _score_snippet(query, snippet, model=model)

        try:
            conn.execute(
                f"INSERT OR REPLACE INTO {_PRECOMPUTE_TABLE} "
                "(memory_id, query_hash, score, snippet, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (memory_id, query_hash, answer_score, snippet, now),
            )
            scored += 1
        except Exception:
            pass

    return scored


def clear_stale_cache(conn: Any, max_age_days: int = 7) -> int:
    """Remove stale entries from the answer rerank cache."""
    cutoff = time.time() - (max_age_days * 86400)
    cur = conn.execute(
        f"DELETE FROM {_PRECOMPUTE_TABLE} WHERE created_at < ?", (cutoff,)
    )
    return int(cur.rowcount)
