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

from __future__ import annotations

from _flock import acquire_lock_or_exit
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

logger = logging.getLogger(__name__)

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))


MIN_IMPRESSIONS = 500
MIN_CLICKS = 10  # Need at least some positive labels


def _build_training_data(db_path: str) -> tuple[list[list[float]], list[int], list[str]]:
    """Build training data from CTR feedback.

    Loads actual content, tags, and metadata from tenant_memories so
    content-dependent features (tag_overlap, query_coverage, exact_phrase,
    ce_weak_first500, content_length) are real values, not zeros.

    Returns:
        X: Feature matrix (list of feature dicts)
        y: Labels (2=clicked, 1=returned, 0=dismissed) for graded LambdaMART
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

        # Batch-load memory metadata for all impression IDs
        all_ids = [r["id"] for r in rows]
        if all_ids:
            ph = ",".join("?" * len(all_ids))
            mem_rows = conn.execute(
                f"SELECT id, content, source_file, tags, created, "
                f"COALESCE(fitness_score, 0.5) as fitness, "
                f"COALESCE(importance, 3) as importance, "
                f"COALESCE(pinned, 0) as pinned, "
                f"last_accessed "
                f"FROM tenant_memories "
                f"WHERE id IN ({ph}) AND deleted_at IS NULL",
                all_ids,
            ).fetchall()
        else:
            mem_rows = []

        mem_map: dict[str, dict] = {}
        for m in mem_rows:
            mem_map[m["id"]] = {
                "content": m["content"] or "",
                "source_file": m["source_file"] or "",
                "tags": m["tags"] or "[]",
                "created": m["created"] or "",
                "fitness": m["fitness"],
                "importance": m["importance"],
                "pinned": m["pinned"],
                "last_accessed": m["last_accessed"],
            }

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
                mid = row["id"]
                meta = mem_map.get(mid, {})

                # Build candidate with actual content from DB
                c = SimpleNamespace(
                    id=mid,
                    content=meta.get("content", ""),
                    source_file=meta.get("source_file", ""),
                    tags=meta.get("tags", "[]"),
                    created=meta.get("created", ""),
                    rank=0.0,
                    final_score=0.0,
                    fitness=meta.get("fitness", 0.5),
                    importance=meta.get("importance", 3),
                    pinned=meta.get("pinned", 0),
                    last_accessed=meta.get("last_accessed", None),
                )

                # Extract features with real content
                feats = extract_ltr_features(c, qid, db=conn)

                # Graded labels: clicked=2, returned=1, dismissed=0
                # LambdaMART uses these as relevance grades, not binary
                if row["clicked_at"]:
                    label = 2
                elif row["dismissed_at"]:
                    label = 0
                else:
                    label = 1

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
    y_arr = np.array(y, dtype=np.int32)  # Graded: 0=dismissed, 1=returned, 2=clicked

    # Build group array for LambdaMART (queries -> result count)
    from collections import OrderedDict
    unique_qids = list(OrderedDict.fromkeys(query_ids))
    group_counts = [query_ids.count(qid) for qid in unique_qids]

    if dry_run:
        return {
            "status": "dry_run",
            "samples": len(X_arr),
            "features": X_arr.shape[1],
            "queries": len(group_counts),
            "clicks": int((y_arr == 2).sum()),
            "total": len(y_arr),
        }

    # Held-out group split: last 20% of unique queries for validation
    split_idx = max(1, int(len(unique_qids) * 0.8))
    train_qids = set(unique_qids[:split_idx])
    val_qids = set(unique_qids[split_idx:])

    train_mask = np.array([qid in train_qids for qid in query_ids])
    val_mask = np.array([qid in val_qids for qid in query_ids])

    # Build group arrays for train and val
    train_groups = [query_ids.count(qid) for qid in unique_qids if qid in train_qids]
    val_groups = [query_ids.count(qid) for qid in unique_qids if qid in val_qids]

    # Train LambdaMART with graded relevance labels
    model_dir.mkdir(parents=True, exist_ok=True)

    dtrain = lgb.Dataset(X_arr[train_mask], label=y_arr[train_mask], group=train_groups)
    dval = lgb.Dataset(X_arr[val_mask], label=y_arr[val_mask], group=val_groups,
                       reference=dtrain)

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
        valid_sets=[dval],
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
        "clicks": int((y_arr == 2).sum()),
        "total": len(y_arr),
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
