"""LTR scoring stage — LambdaMART reranking for the search pipeline.

This module applies a trained LightGBM LambdaMART model to rerank
search results.  It slots in AFTER all CE / late-interaction /
answer_rerank work, writing r[6] (final_score) exactly once.

RANK-FIRST LOCK (PR1.1): The LTR stage takes over the ordering.
CE / late-interaction become features into LTR, not final rank owners.

Usage:
    from search.ltr.scorer import ltr_rerank
    results = ltr_rerank(query, results, db, db_path)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from infra.db import AnyConnection

logger = logging.getLogger(__name__)

_MODEL_PATH = Path(__file__).parent.parent.parent / "models" / "ltr" / "model.txt"
_model = None
_model_load_attempted = False


def _load_model():
    """Lazy-load the LightGBM model."""
    global _model, _model_load_attempted
    if _model_load_attempted:
        return _model
    _model_load_attempted = True

    if not _MODEL_PATH.exists():
        logger.debug("LTR model not found at %s", _MODEL_PATH)
        return None

    try:
        import lightgbm as lgb
        _model = lgb.Booster(model_file=str(_MODEL_PATH))
        logger.info("LTR model loaded from %s", _MODEL_PATH)
        return _model
    except ImportError:
        logger.warning("lightgbm not installed; LTR scoring disabled")
        return None
    except Exception as e:
        logger.warning("Failed to load LTR model: %s", e)
        return None


def ltr_enabled() -> bool:
    """Check if LTR scoring is available and enabled."""
    if os.environ.get("MEMORY_LTR_DISABLED", "").lower() in ("1", "true", "yes"):
        return False
    model = _load_model()
    return model is not None


def ltr_rerank(
    query: str,
    results: list,
    db: "AnyConnection | None" = None,
    db_path: "Path | None" = None,
    limit: int = 30,
    session_ctx: dict | None = None,
) -> list:
    """Apply LTR reranking to search results.

    Takes the current results (already reranked by CE/late-interaction),
    extracts LTR features for each, scores them with the LightGBM model,
    and writes r[6] (final_score) exactly once.

    When LTR is disabled or no model exists, returns results unchanged.

    Args:
        query: The original search query.
        results: List of result tuples (mutable — r[6] will be overwritten).
        db: Database connection for KG features.
        db_path: Database path (for feature extraction).
        limit: Max results to return.
        session_ctx: Session context dict with prior_clicked_ids and
            prior_returned_ids for session-aware features.

    Returns:
        Results sorted by LTR score (r[6]), truncated to limit.
    """
    if not results:
        return results

    model = _load_model()
    if model is None:
        return results

    from search.ltr.features import extract_ltr_features, feature_names

    # Extract features for each candidate
    feature_matrix = []
    for r in results:
        try:
            feats = extract_ltr_features(
                r, query, db=db, now_ts=None, session_ctx=session_ctx,
            )
            feature_matrix.append(feats)
        except Exception as e:
            logger.debug("LTR feature extraction failed for %s: %s",
                        getattr(r, "id", "?"), e)
            # Fill with zeros on failure
            feature_matrix.append({k: 0.0 for k in feature_names()})

    # Build feature array in canonical order
    fnames = feature_names()
    import numpy as np
    X = np.array([[f.get(k, 0.0) for k in fnames] for f in feature_matrix],
                 dtype=np.float32)

    # Score with LightGBM
    try:
        scores = model.predict(X)
    except Exception as e:
        logger.warning("LTR prediction failed: %s", e)
        return results

    # Write r[6] (final_score) exactly once — RANK-FIRST LOCK
    for i, r in enumerate(results):
        r[6] = float(scores[i])

    # Sort by LTR score
    results.sort(key=lambda r: float(r[6]) if r[6] is not None else 0.0,
                 reverse=True)

    return results[:limit]


def ltr_feature_importance() -> dict[str, float] | None:
    """Return feature importance from the trained model.

    Returns a dict of feature_name -> importance value, sorted by
    importance descending.  Useful for the dashboard.
    """
    model = _load_model()
    if model is None:
        return None

    from search.ltr.features import feature_names
    fnames = feature_names()
    importance = model.feature_importance(importance_type="gain")
    return dict(sorted(zip(fnames, importance), key=lambda x: -x[1]))
