#!/usr/bin/env python3
"""Weekly: train TemporalAttentionModel weights from CTR feedback.

Reads memory_ctr_feedback (clicked_at = positive, dismissed_at = negative)
from the last 30 days, computes the 6-dim feature vector that matches the
inference-time layout in search/scoring.py:_ssm_input_vector, runs SGD on the
1-hidden-layer SSM (8 hidden units, 6 inputs → 58 weights total), and writes
the resulting weight vector to config.features.temporal_ssm_weights.

No-op if fewer than 10 labeled examples exist (cold start guard).
The feature flag temporal_ssm_enabled stays OFF until an operator enables it;
writing weights while the flag is off is harmless (the model is only read when
the flag is on, and its score() saturates to 0.5 → neutral rerank until then).

Existing weights are preserved on failure (the write is atomic: temp file +
rename).
"""

from __future__ import annotations

import json
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
_EPOCHS = 120
_LR = 0.5
_L2 = 1e-3
_HIDDEN = 8
_INPUT_DIM = 6
_W_TOTAL = _HIDDEN + 1 + _HIDDEN * _INPUT_DIM + 1  # 8 + 1 + 48 + 1 = 58
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
    except Exception as e:
        logger.warning("_load_examples failed: %s", e)
        return []

    # Most recent search query, used as the surprise proxy (parity with inference).
    recent = None
    try:
        r = conn.execute(
            "SELECT params FROM audit_log WHERE tool_name = 'memory_search' "
            "AND params IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if r:
            params = json.loads(r[0])
            recent = params.get("query", "") if isinstance(params, dict) else ""
    except Exception:
        recent = None

    examples = []
    for row in rows:
        note_id, clicked_at, dismissed_at, returned_at, content, fitness, importance, access_count, last_accessed = row
        # Binary label: clicked = 1, dismissed = 0.
        label = 1.0 if clicked_at is not None else 0.0

        access_signal = min((access_count or 1) / _ACCESS_CAP, 1.0)
        importance_norm = (importance or 3) / 5.0
        fitness_val = float(fitness if fitness is not None else 0.5)

        recency_days = 0.0
        if last_accessed:
            try:
                ts = __import__("datetime").datetime.fromisoformat(last_accessed).timestamp()
                recency_days = max(0.0, (time.time() - ts) / 86400.0)
            except (ValueError, TypeError):
                pass
        recency_penalty = min(recency_days / _RECENCY_CAP, 1.0)

        query_surprise = 0.5
        if recent and content:
            c_words = set(content.lower().split())
            q_words = set(recent.lower().split())
            if c_words and q_words:
                query_surprise = 1.0 - len(c_words & q_words) / len(c_words | q_words)

        q = np.array(
            [access_signal, query_surprise, importance_norm, fitness_val, recency_penalty, 0.0],
            dtype=float,
        )
        examples.append((q, label))
    return examples


def _unpack(weights: np.ndarray):
    W_readout = weights[0:_HIDDEN].reshape(_HIDDEN)
    b_readout = float(weights[_HIDDEN])
    W_input = weights[_HIDDEN + 1:_HIDDEN + 1 + _HIDDEN * _INPUT_DIM].reshape(_HIDDEN, _INPUT_DIM)
    b_input = float(weights[_HIDDEN + 1 + _HIDDEN * _INPUT_DIM])
    return W_readout, b_readout, W_input, b_input


def _pack(W_readout, b_readout, W_input, b_input) -> np.ndarray:
    parts = [W_readout.ravel(), np.array([b_readout]), W_input.ravel(), np.array([b_input])]
    return np.concatenate(parts)


def _sgd_train(X: np.ndarray, y: np.ndarray, epochs: int = _EPOCHS, lr: float = _LR) -> np.ndarray:
    """Mean-loss SGD on the SSM forward pass (matches scoring.TemporalAttentionModel).

    Forward:  h = tanh(W_input @ q + b_input);  pred = tanh(W_readout @ h + b_readout) * 0.5 + 0.5
    Loss:     mean binary cross-entropy vs label.

    Mean reduction (divide gradients by N) is essential: b_input is a scalar
    shared across all hidden units, so a sum-loss gradient would be N*H times
    larger than W_input's and explode (saturating tanh, killing the gradient).
    """
    N, D = X.shape
    # Small random init for W_input/b_input breaks the zero-init dead zone
    # (a fully-zero hidden state sends no gradient back to W_input).
    rng = np.random.default_rng(0)
    W_readout = np.zeros(_HIDDEN, dtype=float)
    b_readout = 0.0
    W_input = rng.uniform(-0.1, 0.1, size=(_HIDDEN, _INPUT_DIM))
    b_input = float(rng.uniform(-0.1, 0.1))

    for _ in range(epochs):
        inner = X @ W_input.T + b_input            # (N, HIDDEN)
        h = np.tanh(inner)                          # (N, HIDDEN)
        raw = h @ W_readout + b_readout             # (N,)
        pred = np.tanh(raw) * 0.5 + 0.5             # (N,)
        pred = np.clip(pred, 1e-6, 1.0 - 1e-6)

        # dL/draw  (mean loss → divide by N implicitly via .mean())
        d_pred = -(y / pred - (1.0 - y) / (1.0 - pred))
        d_raw = d_pred * 0.5 * (1.0 - np.tanh(raw) ** 2)   # (N,)

        dW_readout = (d_raw[:, None] * h).mean(axis=0)
        db_readout = d_raw.mean()
        d_h = d_raw[:, None] * W_readout[None, :]          # (N, HIDDEN)
        d_inner = d_h * (1.0 - h ** 2)                      # (N, HIDDEN)

        dW_input = d_inner.T @ X / N                        # (HIDDEN, INPUT_DIM)
        db_input = d_inner.mean()                           # scalar (b_input broadcasts)

        W_readout -= lr * (dW_readout + _L2 * W_readout)
        b_readout -= lr * db_readout
        W_input -= lr * (dW_input + _L2 * W_input)
        b_input -= lr * (db_input + _L2 * b_input)

    return _pack(W_readout, b_readout, W_input, b_input)


def _write_weights(weights: np.ndarray) -> None:
    cfg = _get_config()
    toml_path = Path(getattr(cfg, "_config_path", "memory.toml")).resolve()
    weights_str = ",".join(f"{v:.6f}" for v in weights)

    content = toml_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    out = []
    replaced = False
    for line in lines:
        if line.strip().startswith("temporal_ssm_weights"):
            out.append(f'temporal_ssm_weights = "{weights_str}"')
            replaced = True
        else:
            out.append(line)
    if not replaced:
        # Append into the [features] block.
        inserted = False
        for i, line in enumerate(out):
            if line.strip() == "[features]":
                out.insert(i + 1, f'temporal_ssm_weights = "{weights_str}"')
                inserted = True
                break
        if not inserted:
            out.append(f"\n[features]\ntemporal_ssm_weights = \"{weights_str}\"")
    new_content = "\n".join(out)

    tmp = toml_path.with_suffix(".toml.tmp")
    tmp.write_text(new_content, encoding="utf-8")
    tmp.replace(toml_path)


def main() -> int:
    os.chdir(str(Path(__file__).resolve().parent.parent))
    setup_logging(__name__, level="INFO", fmt="%(asctime)s %(levelname)s %(message)s")
    acquire_lock_or_exit("cron_train_temporal_ssm")

    db_path = _db_path()
    if not db_path.exists():
        logger.info("cron_train_temporal_ssm: db not found at %s", db_path)
        return 0

    import sqlite3
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    examples = _load_examples(conn)
    conn.close()
    if len(examples) < _MIN_EXAMPLES:
        logger.info("cron_train_temporal_ssm: only %d examples (need >=%d), skipping",
                    len(examples), _MIN_EXAMPLES)
        return 0

    X = np.stack([q for q, _ in examples])
    y = np.array([label for _, label in examples], dtype=float)

    weights = _sgd_train(X, y)
    if weights.size != _W_TOTAL:
        logger.error("cron_train_temporal_ssm: weight count %d != %d", weights.size, _W_TOTAL)
        return 1

    _write_weights(weights)
    logger.info("cron_train_temporal_ssm: trained on %d examples, %d weights written",
                len(examples), weights.size)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        logger.exception("cron_train_temporal_ssm failed: %s", exc)
        sys.exit(1)
