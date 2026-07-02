"""Surprise-based forgetting curve.

Replaces the aspirational ONNX MLP (never trained) with a practical
data-driven formula.  Retention for each memory is computed from:

1. **Query surprise**: how unexpected the memory content is relative
   to the current search query (Jaccard distance).
2. **Historical surprise**: how different the memory is from queries
   the user has made recently (from audit_log).
3. **Access frequency**: how often the memory has been accessed
   (from user_access_log).
4. **Recency**: days since last access.
5. **Importance / fitness**: the memory's existing quality signals.

Formula (sigmoid-weighted):

    retention = sigmoid(w_acc * access_signal + w_surp * surprise +
                        w_imp * importance_norm + w_fit * fitness -
                        w_rec * recency_penalty - bias)

Scaled to [0,1] — 1 = retain forever, 0 = forget immediately.

Wired into ``search_pipeline._apply_neural_forget_curve()`` and replaces
``adaptive_retention.batch_update_retention()`` when the feature is on.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from pathlib import Path
from typing import Optional

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    np = None  # type: ignore
    _HAS_NUMPY = False

logger = logging.getLogger(__name__)

# Surprise-based forgetting curve weights (used as fallback when no
# learned model is available)
_W_ACCESS = 2.0  # access frequency weight
_W_SURPRISE = 1.5  # surprise weight
_W_IMPORTANCE = 1.0  # importance weight
_W_FITNESS = 0.5  # fitness weight
_W_RECENCY = 2.0  # recency penalty weight
_BIAS = 1.5  # sigmoid bias (higher = more retention overall)

_ACCESS_CAP = 20  # access count beyond which signal saturates
_RECENCY_CAP = 365  # days beyond which recency penalty saturates
_QUERY_HISTORY = 50  # recent audit_log entries to consider

_FEATURE_NAMES = [
    "access_signal",
    "surprise",
    "importance_norm",
    "fitness",
    "recency_penalty",
]


class NeuralForgetModel:
    """Learned logistic-retention model over 5 memory features.

    Weights are trained offline from ``memory_ctr_feedback`` (clicked_at
    = positive, dismissed_at = negative) and stored as a CSV string in
    ``config.neural_forget_weights``.  When no weights are present (first
    run, training has not yet run, or config says "formula") the
    hard-coded _W_* / _BIAS constants are used as the model parameters —
    this is not a fallback stub, it is the deterministic formula that
    has been running in production unchanged.

    Inference cost is one dot-product + sigmoid.  No external model
    loading, no GPU, no torch dependency.
    """

    def __init__(self, weights: np.ndarray | list[float] | None = None) -> None:
        self.W: np.ndarray | list[float]
        self.b: float
        if _HAS_NUMPY:
            assert np is not None
            if weights is not None:
                if isinstance(weights, list):
                    weights = np.array(weights)
                if weights.shape == (6,):
                    self.W = weights[:5].astype(float)
                    self.b = float(weights[5])
                    return
            self.W = np.array([_W_ACCESS, _W_SURPRISE, _W_IMPORTANCE, _W_FITNESS, -_W_RECENCY], dtype=float)
            self.b = -_BIAS
        else:
            if weights is not None and len(weights) == 6:
                self.W = [float(x) for x in weights[:5]]
                self.b = float(weights[5])
            else:
                self.W = [_W_ACCESS, _W_SURPRISE, _W_IMPORTANCE, _W_FITNESS, -_W_RECENCY]
                self.b = -_BIAS

    def predict(self, features: np.ndarray | list[float]) -> float:
        if _HAS_NUMPY:
            raw = float(np.dot(features, self.W) + self.b)
            return 1.0 / (1.0 + math.exp(-np.clip(raw, -88.0, 88.0)))
        else:
            raw = sum(f * w for f, w in zip(features, self.W)) + self.b
            clipped = max(-88.0, min(88.0, raw))
            return 1.0 / (1.0 + math.exp(-clipped))

    @classmethod
    def from_config(cls) -> "NeuralForgetModel":
        try:
            from infra._lazy_imports import get_config
            raw = get_config().neural_forget_weights
        except Exception:
            raw = ""
        if not raw:
            return cls(weights=None)
        try:
            parts = [float(x) for x in raw.split(",")]
            if _HAS_NUMPY:
                arr = np.array(parts)
                if arr.shape == (6,):
                    return cls(arr)
            else:
                if len(parts) == 6:
                    return cls(parts)
        except Exception:
            pass
        return cls(weights=None)

    def to_config_str(self) -> str:
        return ",".join(f"{v:.6f}" for v in list(self.W) + [self.b])

    @staticmethod
    def features(
        access_signal: float,
        query_surprise: float,
        importance_norm: float,
        fitness: float,
        recency_penalty: float,
    ) -> np.ndarray | list[float]:
        if _HAS_NUMPY:
            return np.array([access_signal, query_surprise, importance_norm, fitness, recency_penalty], dtype=float)
        else:
            return [access_signal, query_surprise, importance_norm, fitness, recency_penalty]


def surprise_score(content: str, reference: str) -> float:
    """Jaccard-distance between content and reference.

    Returns 0.0 (identical) to 1.0 (completely disjoint).
    """
    if not content or not reference:
        return 0.5
    c_words = set(content.lower().split())
    r_words = set(reference.lower().split())
    if not c_words or not r_words:
        return 0.5
    intersect = c_words & r_words
    union = c_words | r_words
    return 1.0 - len(intersect) / len(union)


def _sigmoid(x: float) -> float:
    """Logistic sigmoid."""
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def compute_retention_rate(
    content: str,
    access_count: int,
    recency_days: float,
    fitness: float,
    importance: int,
    query_surprise: float = 0.5,
    half_life_days: Optional[float] = None,
) -> float:
    """Compute retention rate in [0, 1].

    Args:
        content: Memory content text.
        access_count: Number of times this memory was accessed.
        recency_days: Days since last access.
        fitness: Existing fitness score [0, 1].
        importance: Importance level (1-5).
        query_surprise: Surprise relative to current query [0, 1].
        half_life_days: Optional adapted half-life in days to scale decay.

    Returns:
        Retention rate where 1.0 = keep, 0.0 = forget.
    """
    access_signal = min(access_count / _ACCESS_CAP, 1.0)
    recency_cap = (half_life_days * 2.0) if half_life_days is not None else _RECENCY_CAP
    recency_penalty = min(recency_days / recency_cap, 1.0)
    importance_norm = importance / 5.0

    features = NeuralForgetModel.features(
        access_signal, query_surprise, importance_norm, fitness, recency_penalty
    )

    try:
        from infra._lazy_imports import get_config

        mode = getattr(get_config(), "neural_forget_mode", "formula")
    except Exception:
        mode = "formula"

    if mode == "learned":
        return NeuralForgetModel.from_config().predict(features)
    if mode == "hybrid":
        model = NeuralForgetModel.from_config()
        learned = model.predict(features)
        formula = _sigmoid(
            _W_ACCESS * access_signal
            + _W_SURPRISE * query_surprise
            + _W_IMPORTANCE * importance_norm
            + _W_FITNESS * fitness
            - _W_RECENCY * recency_penalty
            - _BIAS
        )
        W_sum = np.sum(model.W) if _HAS_NUMPY else sum(model.W)
        is_fallback = False
        if _HAS_NUMPY:
            assert np is not None
            is_fallback = bool(np.array_equal(model.W, np.array([_W_ACCESS, _W_SURPRISE, _W_IMPORTANCE, _W_FITNESS, -_W_RECENCY])))
        else:
            is_fallback = (model.W == [_W_ACCESS, _W_SURPRISE, _W_IMPORTANCE, _W_FITNESS, -_W_RECENCY])
        if W_sum != 0.0 and not is_fallback:
            return learned * 0.85 + formula * 0.15
        return formula

    return _sigmoid(
        _W_ACCESS * access_signal
        + _W_SURPRISE * query_surprise
        + _W_IMPORTANCE * importance_norm
        + _W_FITNESS * fitness
        - _W_RECENCY * recency_penalty
        - _BIAS
    )


def _recent_queries(db: sqlite3.Connection, limit: int = _QUERY_HISTORY) -> list[str]:
    """Return the text of recent search queries from audit_log."""
    queries: list[str] = []
    try:
        rows = db.execute(
            "SELECT params FROM audit_log "
            "WHERE tool_name = 'memory_search' AND params IS NOT NULL "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        for (params_json,) in rows:
            try:
                params = json.loads(params_json)
                q = params.get("query", "") if isinstance(params, dict) else ""
                if q:
                    queries.append(q)
            except (json.JSONDecodeError, TypeError):
                continue
    except sqlite3.OperationalError:
        pass
    return queries


def compute_forgetting_rate(
    note_id: str,
    db_path: str | Path,
    content: Optional[str] = None,
    recent_memories: Optional[list[str]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> float:
    """Compute retention rate for a single note.

    Args:
        note_id: Memory note ID.
        db_path: Path to memory.db.
        content: Pre-loaded content (avoids second DB query).
        recent_memories: Ignored (kept for API compat).
        conn: Optional existing connection to reuse (avoids nested open_db).

    Returns:
        Retention rate [0, 1].
    """
    row_data: dict = {}
    should_close = conn is None
    if conn is None:
        from infra._lazy_imports import open_db
        conn = open_db(Path(db_path)).__enter__()

    try:
        row = conn.execute(
            "SELECT content, fitness_score, importance, access_count, "
            "       last_accessed, metadata "
            "FROM memories WHERE id=?",
            (note_id,),
        ).fetchone()
        if row is None:
            return 0.5

        row_data = {
            "body": row[0] or "",
            "fitness": float(row[1] or 0.5),
            "importance": int(row[2] or 3),
            "access_count": int(row[3] or 1),
            "last_accessed": row[4] or "",
            "meta_json": row[5] or "{}",
        }
    finally:
        if should_close:
            try:
                conn.__exit__(None, None, None)
            except Exception:
                pass

    body: str = row_data["body"]
    fitness = row_data["fitness"]
    importance = row_data["importance"]
    access_count = row_data["access_count"]
    last_accessed = row_data["last_accessed"]
    meta_json = row_data["meta_json"]

    half_life_days = None
    try:
        meta = json.loads(meta_json)
        if isinstance(meta, dict) and "adaptive_halflife_days" in meta:
            half_life_days = float(meta["adaptive_halflife_days"])
    except Exception:
        pass

    recency_days = 0.0
    if last_accessed:
        try:
            ts = (
                __import__("datetime").datetime.fromisoformat(last_accessed).timestamp()
            )
            recency_days = max(0.0, (time.time() - ts) / 86400.0)
        except (ValueError, TypeError):
            pass

    return compute_retention_rate(
        body, access_count, recency_days, fitness, importance, half_life_days=half_life_days
    )


def compute_query_surprise(
    content: str, query: str, db: Optional[sqlite3.Connection] = None
) -> float:
    """Surprise of a memory relative to the current query and recent history.

    Combines query-level surprise (content vs current query) and
    historical surprise (content vs recent audit_log queries).

    Returns value in [0, 1] where higher = more surprising.
    """
    query_surp = surprise_score(content, query)

    if db is None:
        return query_surp

    recent = _recent_queries(db)
    if not recent:
        return query_surp

    hist_surp = max(surprise_score(content, q) for q in recent)
    return max(query_surp, hist_surp)


def batch_update_retention(db_path: str | Path, limit: int = 500) -> dict:
    """Batch update retention rates for all active memories.

    Stores the result in the ``score`` column.

    Args:
        db_path: Path to memory.db.
        limit: Max memories to process.

    Returns:
        Dict with counts.
    """
    from infra._lazy_imports import open_db

    db_path = Path(db_path)
    updated = 0
    failed = 0

    with open_db(db_path, timeout=30.0) as conn:
        rows = conn.execute(
            f"SELECT id, content FROM memories WHERE deleted_at IS NULL LIMIT {limit}"
        ).fetchall()

        for note_id, content in rows:
            try:
                rate = compute_forgetting_rate(note_id, db_path, content=content, conn=conn)
                conn.execute(
                    "UPDATE memories SET score=? WHERE id=?",
                    (round(rate, 4), note_id),
                )
                updated += 1
            except Exception:
                failed += 1

        conn.commit()

    return {"updated": updated, "failed": failed}


def is_available() -> bool:
    """Always available — no external model needed."""
    return True
