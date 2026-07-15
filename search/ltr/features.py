"""LTR feature extraction — read-only signal extraction for LambdaMART.

Extracts ~25 features per candidate result from existing search pipeline
signals.  All functions are pure readers; none mutate r[6] or any result
tuple, preserving the RANK-FIRST LOCK (PR1.1).

Usage:
    from search.ltr.features import extract_ltr_features
    features = extract_ltr_features(candidate, query, db)
"""

from __future__ import annotations

import json
import math
import re
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from infra.db import AnyConnection

# Query token regex — same as scoring.py
_RERANK_TOKEN_RE = re.compile(r"[A-Za-z0-9#@+][A-Za-z0-9\-_/+#]{2,}")


def _tokenize(text: str) -> list[str]:
    """Lowercase tokenization matching the scoring pipeline."""
    return [t.lower() for t in _RERANK_TOKEN_RE.findall(text) if len(t) >= 3]


def _sigmoid(rank: float) -> float:
    """Convert raw BM25 rank to [0, 1] via sigmoid."""
    rank = max(-60.0, min(60.0, float(rank) if rank is not None else 0.0))
    return 1.0 / (1.0 + math.exp(rank))


_default_session_ctx: dict = {}


def extract_ltr_features(
    candidate,
    query: str,
    db: "AnyConnection | None" = None,
    query_tokens: set[str] | None = None,
    channel_scores: dict[str, float] | None = None,
    now_ts: float | None = None,
    session_ctx: dict | None = None,
) -> dict[str, float]:
    """Extract LTR features for a single candidate result.

    Args:
        candidate: A MemoryResultRow or similar tuple with fields:
            id, content, source_file, tags, created, rank, final_score,
            fitness, importance, pinned, last_accessed, metadata
        query: The original search query.
        db: Database connection for KG features (optional).
        query_tokens: Pre-computed query token set (optional).
        channel_scores: Per-channel scores from the search pipeline (optional).
            Expected keys: bm25, semantic, splade, ce_weak, ce_chunk,
            late_interaction, kg_1hop, kg_2hop
        now_ts: Current timestamp (optional, defaults to time.time()).

    Returns:
        Dict of feature_name -> float value. All values are numeric (0-1
        range where possible) for direct consumption by LightGBM.
    """
    if now_ts is None:
        now_ts = time.time()

    features: dict[str, float] = {}

    # --- Pre-compute query tokens ---
    if query_tokens is None:
        query_tokens = set(_tokenize(query))
    query_token_count = max(1, len(query_tokens))

    # --- Extract candidate fields ---
    mid = getattr(candidate, "id", "") or ""
    content = getattr(candidate, "content", "") or ""
    source_file = getattr(candidate, "source_file", "") or ""
    tags_raw = getattr(candidate, "tags", "") or ""
    created = getattr(candidate, "created", "") or ""
    rank = getattr(candidate, "rank", 0.0)
    fitness = getattr(candidate, "fitness", None)
    importance = getattr(candidate, "importance", None)
    pinned = getattr(candidate, "pinned", None)
    last_accessed = getattr(candidate, "last_accessed", None)

    # --- 1. BM25 normalized (sigmoid of raw rank) ---
    features["bm25_norm"] = _sigmoid(rank)

    # --- 2. Fitness score ---
    features["fitness"] = float(fitness) if fitness is not None else 0.5

    # --- 3. Importance (normalized /5) ---
    features["importance"] = (float(importance) / 5.0) if importance is not None else 0.6

    # --- 4. Pinned (binary) ---
    features["pinned"] = 1.0 if pinned else 0.0

    # --- 5. Tag overlap (fraction of query tokens in tags) ---
    tag_match = 0.0
    if tags_raw and query_tokens:
        try:
            tags_list = json.loads(tags_raw) if isinstance(tags_raw, str) else []
        except Exception:
            tags_list = []
        tag_tokens: set[str] = set()
        for t in tags_list:
            if isinstance(t, str):
                for token in _RERANK_TOKEN_RE.findall(t):
                    if len(token) >= 3:
                        tag_tokens.add(token.lower())
        if tag_tokens:
            hits = len(query_tokens & tag_tokens)
            tag_match = min(1.0, hits / query_token_count)
    features["tag_overlap"] = tag_match

    # --- 6. Query coverage (fraction of query tokens in content) ---
    content_tokens = set(_tokenize(content[:2000])) if content else set()
    if query_tokens and content_tokens:
        features["query_coverage"] = len(query_tokens & content_tokens) / query_token_count
    else:
        features["query_coverage"] = 0.0

    # --- 7. Exact phrase match (1.0 if query appears in content) ---
    content_lower = content[:5000].lower() if content else ""
    features["exact_phrase"] = 1.0 if query.lower() in content_lower else 0.0

    # --- 8. Content length (log-normalized, capped at 10k chars) ---
    content_len = len(content) if content else 0
    features["content_length"] = min(1.0, math.log1p(content_len) / math.log1p(10000))

    # --- 9. Age (days since creation, log-normalized) ---
    age_days = 0.0
    if created:
        try:
            from datetime import datetime, timezone
            if "T" in created:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            else:
                dt = datetime.strptime(created[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            age_days = max(0.0, (now_ts - dt.timestamp()) / 86400.0)
        except Exception:
            age_days = 0.0
    features["age_days"] = min(1.0, math.log1p(age_days) / math.log1p(365))

    # --- 10. Last accessed recency (days since last access, log-normalized) ---
    access_days = 365.0  # default: never accessed
    if last_accessed:
        try:
            from datetime import datetime, timezone
            if "T" in str(last_accessed):
                dt = datetime.fromisoformat(str(last_accessed).replace("Z", "+00:00"))
            else:
                dt = datetime.fromtimestamp(float(last_accessed), tz=timezone.utc)
            access_days = max(0.0, (now_ts - dt.timestamp()) / 86400.0)
        except Exception:
            access_days = 365.0
    features["recency"] = 1.0 - min(1.0, math.log1p(access_days) / math.log1p(365))

    # --- 11. Source file type (one-hot: session, lesson, decision, concept, other) ---
    sf_lower = source_file.lower()
    features["is_session"] = 1.0 if "session" in sf_lower else 0.0
    features["is_lesson"] = 1.0 if "lesson" in sf_lower else 0.0
    features["is_decision"] = 1.0 if "decision" in sf_lower else 0.0
    features["is_concept"] = 1.0 if "concept" in sf_lower else 0.0

    # --- 12. Channel scores from search pipeline ---
    if channel_scores:
        features["channel_bm25"] = channel_scores.get("bm25", 0.0)
        features["channel_semantic"] = channel_scores.get("semantic", 0.0)
        features["channel_splade"] = channel_scores.get("splade", 0.0)
        features["channel_ce_weak"] = channel_scores.get("ce_weak", 0.0)
        features["channel_ce_chunk"] = channel_scores.get("ce_chunk", 0.0)
        features["channel_late_interaction"] = channel_scores.get("late_interaction", 0.0)
        features["channel_kg_1hop"] = channel_scores.get("kg_1hop", 0.0)
        features["channel_kg_2hop"] = channel_scores.get("kg_2hop", 0.0)
    else:
        for ch in ("bm25", "semantic", "splade", "ce_weak", "ce_chunk",
                    "late_interaction", "kg_1hop", "kg_2hop"):
            features[f"channel_{ch}"] = 0.0

    # --- 12b. Second CE feature: weak CE on first 500 chars ---
    # This captures whether the query matches the document's opening
    # (which is often the most relevant part for short queries).
    if content:
        from search.rerankers import _cross_encoder_score
        features["ce_weak_first500"] = _cross_encoder_score(query, content[:500])
    else:
        features["ce_weak_first500"] = 0.0

    # --- 13. KG features (if db available) ---
    if db is not None and mid:
        try:
            # 1-hop edge count
            row = db.execute(
                "SELECT COUNT(*) FROM kg_edges e "
                "JOIN kg_entities en ON e.source_id = en.id OR e.target_id = en.id "
                "WHERE en.source_memory_id = ? OR en.name LIKE ?",
                (mid, f"%{mid.split('/')[-1] if '/' in mid else mid}%"),
            ).fetchone()
            features["kg_edge_count"] = min(1.0, (row[0] if row else 0) / 10.0)

            # Has KG entities (binary)
            row2 = db.execute(
                "SELECT 1 FROM kg_entities WHERE source_memory_id = ? LIMIT 1",
                (mid,),
            ).fetchone()
            features["has_kg_entity"] = 1.0 if row2 else 0.0
        except Exception:
            features["kg_edge_count"] = 0.0
            features["has_kg_entity"] = 0.0
    else:
        features["kg_edge_count"] = 0.0
        features["has_kg_entity"] = 0.0

    # --- 14. Final score from current pipeline (the score LTR will replace) ---
    features["pipeline_final_score"] = getattr(candidate, "final_score", 0.0) or 0.0

    # --- 15. Session-aware features (from session_ctx) ---
    # session_ctx is an optional dict with:
    #   prior_clicked_ids: set[str] — memory IDs clicked in prior queries
    #   prior_returned_ids: dict[str, float] — {memory_id: avg_seconds_returned}
    if session_ctx is None:
        session_ctx = _default_session_ctx
    prior_returned = session_ctx.get("prior_returned_ids") or {}
    prior_clicked = session_ctx.get("prior_clicked_ids") or set()
    features["was_returned_in_prior"] = 1.0 if mid in prior_returned else 0.0
    features["was_clicked_in_prior"] = 1.0 if mid in prior_clicked else 0.0
    dwell = prior_returned.get(mid, 0.0)
    features["session_dwell"] = min(1.0, dwell / 300.0) if dwell > 0 else 0.0

    return features


def feature_names() -> list[str]:
    """Return the ordered list of feature names.

    Call this to get the canonical feature order for training data
    and LightGBM model input.
    """
    return [
        # Core retrieval signals
        "bm25_norm",
        "fitness",
        "importance",
        "pinned",
        # Text matching
        "tag_overlap",
        "query_coverage",
        "exact_phrase",
        # Content properties
        "content_length",
        "age_days",
        "recency",
        # Source type
        "is_session",
        "is_lesson",
        "is_decision",
        "is_concept",
        # Channel scores from search pipeline
        "channel_bm25",
        "channel_semantic",
        "channel_splade",
        "channel_ce_weak",
        "channel_ce_chunk",
        "channel_late_interaction",
        "channel_kg_1hop",
        "channel_kg_2hop",
        # Second CE feature (different text window)
        "ce_weak_first500",
        # KG features
        "kg_edge_count",
        "has_kg_entity",
        # Session-aware features
        "was_returned_in_prior",
        "was_clicked_in_prior",
        "session_dwell",
        # Pipeline score (for baseline comparison)
        "pipeline_final_score",
    ]
