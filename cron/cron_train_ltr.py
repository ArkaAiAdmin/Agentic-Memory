#!/usr/bin/env python3
"""Weekly LambdaMART trainer for LTR reranking.

Trains a LightGBM LambdaMART model on CTR feedback data and writes
the model to models/ltr/model.txt.  Gated on ≥500 labelled impressions
from memory_ctr_feedback.

Usage:
    venv/bin/python cron/cron_train_ltr.py

Can also be triggered via:
    memory_maintenance(operation="train_ltr")
"""

from _flock import acquire_lock_or_exit
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))


MIN_IMPRESSIONS = 500
MIN_CLICKS = 10  # Need at least some positive labels


def _build_training_data(db_path: str) -> tuple[list[list[float]], list[int], list[str]]:
    """Build training data from CTR feedback.

    Returns:
        X: Feature matrix (list of feature dicts)
        y: Labels (1=clicked, 0=returned-not-clicked, -1=dismissed)
        query_ids: Query IDs for group-based ranking
    """
    import sqlite3
    from search.ltr.features import extract_ltr_features, feature_names

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row

    try:
        # Get all impressions with their labels
        rows = conn.execute(
            "SELECT query_id, id, clicked_at, dismissed_at "
            "FROM memory_ctr_feedback "
            "ORDER BY query_id, returned_at"
        ).fetchall()

        if not rows:
            logger.info("No CTR feedback data")
            return [], [], []

        # Group by query_id
        from collections import defaultdict
        query_groups: dict[str, list] = defaultdict(list)
        for row in rows:
            query_groups[row["query_id"]].append(row)

        X = []
        y = []
        query_ids = []

        for qid, group in query_groups.items():
            for row in group:
                # Build a minimal candidate object for feature extraction
                class _Candidate:
                    pass
                c = _Candidate()
                c.id = row["id"]
                c.content = ""
                c.source_file = ""
                c.tags = "[]"
                c.created = ""
                c.rank = 0.0
                c.final_score = 0.0
                c.fitness = 0.5
                c.importance = 3
                c.pinned = 0
                c.last_accessed = None

                # Extract features (content-based features will be 0
                # since we don't load full content here — channel scores
                # and metadata features are what matter for LTR)
                feats = extract_ltr_features(c, "", db=conn)

                # Label: 1=clicked, 0=returned, -1=dismissed
                if row["clicked_at"]:
                    label = 1
                elif row["dismissed_at"]:
                    label = -1
                else:
                    label = 0

                X.append([feats.get(k, 0.0) for k in feature_names()])
                y.append(label)
                query_ids.append(qid)

        return X, y, query_ids

    finally:
        conn.close()


def train_ltr_model(dry_run: bool = False) -> dict:
    """Train the LambdaMART model and write to models/ltr/model.txt.

    Returns a dict with training stats.
    """
    from infra.infrastructure import resolve_active_memory_dir

    db_path = str(resolve_active_memory_dir() / "memory.db")
    model_dir = Path(__file__).parent.parent / "models" / "ltr"
    model_path = model_dir / "model.txt"

    # Check minimum data requirements
    import sqlite3
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        row = conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN clicked_at IS NOT NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN dismissed_at IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM memory_ctr_feedback"
        ).fetchone()
        total, clicks, dismissals = row[0] or 0, row[1] or 0, row[2] or 0
    finally:
        conn.close()

    if total < MIN_IMPRESSIONS:
        return {
            "status": "skipped",
            "reason": f"Insufficient data: {total} impressions (need {MIN_IMPRESSIONS})",
            "total": total,
            "clicks": clicks,
            "dismissals": dismissals,
        }

    if clicks < MIN_CLICKS:
        return {
            "status": "skipped",
            "reason": f"Insufficient clicks: {clicks} (need {MIN_CLICKS})",
            "total": total,
            "clicks": clicks,
            "dismissals": dismissals,
        }

    # Build training data
    X, y, query_ids = _build_training_data(db_path)
    if not X:
        return {"status": "skipped", "reason": "No training data constructed"}

    try:
        import numpy as np
        import lightgbm as lgb
    except ImportError as e:
        return {"status": "error", "reason": f"Missing dependency: {e}"}

    X_arr = np.array(X, dtype=np.float32)
    y_arr = np.array(y, dtype=np.int32)

    # Map labels: -1 (dismissed) -> 0, 0 (returned) -> 0, 1 (clicked) -> 1
    # LambdaMART needs non-negative labels; we use binary relevance
    y_binary = (y_arr == 1).astype(int)

    # Build group array for LambdaMART (queries -> result count)
    from collections import OrderedDict
    group_counts = []
    for qid in OrderedDict.fromkeys(query_ids):
        count = query_ids.count(qid)
        group_counts.append(count)

    if dry_run:
        return {
            "status": "dry_run",
            "samples": len(X_arr),
            "features": X_arr.shape[1],
            "queries": len(group_counts),
            "clicks": int(y_binary.sum()),
            "total": len(y_binary),
        }

    # Train LambdaMART
    model_dir.mkdir(parents=True, exist_ok=True)

    dtrain = lgb.Dataset(X_arr, label=y_binary, group=group_counts)

    params = {
        "objective": "lambdarank",
        "metric": "ndcg",
        "eval_at": [5, 10],
        "num_leaves": 31,
        "learning_rate": 0.05,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "verbose": -1,
        "min_data_in_leaf": 10,
        "lambda_l1": 0.1,
        "lambda_l2": 0.1,
    }

    model = lgb.train(
        params,
        dtrain,
        num_boost_round=200,
        valid_sets=[dtrain],
        callbacks=[lgb.log_evaluation(0)],
    )

    model.save_model(str(model_path))

    # Report feature importance
    importance = model.feature_importance(importance_type="gain")
    from search.ltr.features import feature_names
    fnames = feature_names()
    top_features = sorted(zip(fnames, importance), key=lambda x: -x[1])[:5]

    return {
        "status": "trained",
        "model_path": str(model_path),
        "samples": len(X_arr),
        "features": X_arr.shape[1],
        "queries": len(group_counts),
        "clicks": int(y_binary.sum()),
        "total": len(y_binary),
        "top_features": [{"name": n, "importance": float(v)} for n, v in top_features],
    }


if __name__ == "__main__":
    acquire_lock_or_exit("cron_train_ltr")
    import argparse
    parser = argparse.ArgumentParser(description="Train LTR model")
    parser.add_argument("--dry-run", action="store_true", help="Preview without training")
    args = parser.parse_args()

    result = train_ltr_model(dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
