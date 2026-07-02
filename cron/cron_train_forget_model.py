#!/usr/bin/env python3
"""Nightly: train NeuralForgetModel weights from CTR feedback.

Reads memory_ctr_feedback (clicked_at = positive, dismissed_at = negative)
from the last 30 days, computes 5 retention features per example, runs
50-epoch SGD, and writes the resulting weight vector to config.neural_forget_weights.

No-op if fewer than 10 labeled examples exist (cold start guard).
Existing oracle weights are preserved on failure (write is atomic).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from _flock import acquire_lock_or_exit

import numpy as np

_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from infra.log import setup_logging
logger = setup_logging(__name__)

_DAYS = 30
_MIN_EXAMPLES = 10
_EPOCHS = 50
_LR = 0.05
_QUERY_HISTORY = 50  # kept for parity with the formula

# ---------------------------------------------------------------------------
# Feature extraction (mirrors neural_forget.compute_retention_rate)
# ---------------------------------------------------------------------------

_ACCESS_CAP = 20
_RECENCY_CAP = 365


def _get_config():
    # Lazy import to avoid boot-time cycle; this cron runs standalone.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from config import get_config
    return get_config()


def _db_path() -> Path:
    cfg = _get_config()
    return Path(cfg.db_path)


def _load_examples(conn):
    cutoff = time.time() - _DAYS * 86400
    try:
        rows = conn.execute(
            """SELECT cf.note_id, cf.clicked_at, cf.dismissed_at, cf.returned_at,
                      m.content, m.fitness_score, m.importance, m.access_count, m.last_accessed
               FROM memory_ctr_feedback cf
               JOIN memories m ON m.id = cf.note_id
               WHERE cf.returned_at > ?
               AND (cf.clicked_at IS NOT NULL OR cf.dismissed_at IS NOT NULL)
               """,
            (cutoff,),
        ).fetchall()
    except Exception:
        return []
    examples = []
    for row in rows:
        note_id, clicked_at, dismissed_at, returned_at, content, fitness, importance, access_count, last_accessed = row
        label = 1.0 if clicked_at is not None else -1.0

        access_signal = min((access_count or 1) / _ACCESS_CAP, 1.0)
        recency_days = 0.0
        if last_accessed:
            try:
                ts = __import__("datetime").datetime.fromisoformat(last_accessed).timestamp()
                recency_days = max(0.0, (time.time() - ts) / 86400.0)
            except (ValueError, TypeError):
                pass
        recency_penalty = min(recency_days / _RECENCY_CAP, 1.0)
        importance_norm = (importance or 3) / 5.0
        fitness_val = float(fitness or 0.5)

        # Surprise uses Jaccard vs most recent query at the time (approximated
        # by current query — most recent search is the best proxy we have).
        recent = conn.execute(
            "SELECT params FROM audit_log WHERE tool_name = 'memory_search' "
            "AND params IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        query_surprise = 0.5
        if recent:
            try:
                params = json.loads(recent[0])
                q = params.get("query", "") if isinstance(params, dict) else ""
                if q and content:
                    c_words = set(content.lower().split())
                    q_words = set(q.lower().split())
                    if c_words and q_words:
                        query_surprise = 1.0 - len(c_words & q_words) / len(c_words | q_words)
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass

        features = np.array([access_signal, query_surprise, importance_norm, fitness_val, recency_penalty], dtype=float)
        examples.append((features, label))
    return examples


def _sgd_train(X: np.ndarray, y: np.ndarray, epochs: int = _EPOCHS, lr: float = _LR) -> np.ndarray:
    """SGD hinge-loss logistic regression. Returns (W, b) packed as (6,)."""
    N, D = X.shape
    W = np.zeros(D, dtype=float)
    b = 0.0
    for _ in range(epochs):
        idx = np.random.permutation(N)
        Xs, ys = X[idx], y[idx]
        for i in range(N):
            margin = ys[i] * (np.dot(W, Xs[i]) + b)
            if margin < 1:
                W += lr * ys[i] * Xs[i]
                b += lr * ys[i]
        W *= 0.999  # L2 decay
    return np.array([*W, b])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    os.chdir(str(Path(__file__).resolve().parent.parent))
    setup_logging(__name__, level="INFO", fmt="%(asctime)s %(levelname)s %(message)s")
    acquire_lock_or_exit("cron_train_forget_model")

    db_path = _db_path()
    if not db_path.exists():
        logger.info("cron_train_forget_model: db not found at %s", db_path)
        return 0

    import sqlite3
    conn = sqlite3.connect(str(db_path))
    examples = _load_examples(conn)
    if len(examples) < _MIN_EXAMPLES:
        logger.info("cron_train_forget_model: only %d examples (need >=%d), skipping",
                     len(examples), _MIN_EXAMPLES)
        return 0

    X = np.stack([f for f, _ in examples])
    y = np.array([label for _, label in examples], dtype=float)

    weights = _sgd_train(X, y)
    weights_str = ",".join(f"{v:.6f}" for v in weights)

    # Atomic write: write to temp, then rename
    cfg = _get_config()
    toml_path = Path(cfg._config_path if hasattr(cfg, "_config_path") else "memory.toml").resolve()
    content = toml_path.read_text(encoding="utf-8")
    if "[features]" in content:
        # Replace or insert neural_forget_weights under [features]
        lines = content.splitlines()
        out = []
        replaced = False
        for line in lines:
            if line.strip().startswith("neural_forget_weights"):
                out.append(f'neural_forget_weights = "{weights_str}"')
                replaced = True
            else:
                out.append(line)
        if not replaced:
            # Find the [features] block and append
            for i, line in enumerate(out):
                if line.strip() == "[features]":
                    out.insert(i + 1, f'neural_forget_weights = "{weights_str}"')
                    break
        new_content = "\n".join(out)
    else:
        new_content = content + f'\n[features]\nneural_forget_weights = "{weights_str}"\n'

    tmp = toml_path.with_suffix(".toml.tmp")
    tmp.write_text(new_content, encoding="utf-8")
    tmp.replace(toml_path)

    logger.info("cron_train_forget_model: trained on %d examples, weights=%s...", len(examples), weights_str[:40])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        logger.exception("cron_train_forget_model failed: %s", exc)
        sys.exit(1)
