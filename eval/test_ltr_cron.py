"""Regression tests for cron/cron_train_ltr.py.

Covers the trained-branch return-dict path that previously raised a
NameError on an undefined ``y_binary`` variable (see fix: now uses y_arr).
"""

import sqlite3
import sys
import types
from pathlib import Path


def _make_feedback_db(path: Path, impressions: int, clicks: int):
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE memory_ctr_feedback (
            id INTEGER PRIMARY KEY,
            query_id TEXT,
            returned_at TEXT,
            clicked_at TEXT,
            dismissed_at TEXT
        )
        """
    )
    for i in range(impressions):
        clicked = "2026-01-01T00:00:00Z" if i < clicks else None
        conn.execute(
            "INSERT INTO memory_ctr_feedback (id, query_id, returned_at, clicked_at, dismissed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (i + 1, f"q{i % 50}", "2026-01-01T00:00:00Z", clicked, None),
        )
    conn.commit()
    conn.close()


class _Booster:
    def __init__(self, n_features: int):
        self._n = n_features

    def save_model(self, path):
        return None

    def feature_importance(self, importance_type=None):
        return [1.0] * self._n

    def predict(self, X):
        return [0.0] * len(X)


class _Dataset:
    def __init__(self, *a, **k):
        pass


def _make_lightgbm_stub(n_features: int):
    stub = types.SimpleNamespace()

    def train(params, dtrain, num_boost_round=None, valid_sets=None, callbacks=None):
        return _Booster(n_features)

    stub.train = train
    stub.Dataset = _Dataset
    stub.log_evaluation = lambda period=None: None
    stub.Booster = _Booster
    return stub


def test_ltr_trained_branch_returns_stats_without_nameerror():
    from search.ltr.features import feature_names

    n_feat = len(feature_names())
    n_impr, n_clicks = 510, 12

    tmp = Path(str(__import__("tempfile").mkdtemp(prefix="ltr_reg_")))
    db_path = tmp / "memory.db"
    _make_feedback_db(db_path, n_impr, n_clicks)

    import cron.cron_train_ltr as cron_mod
    from infra import infrastructure as infra_mod

    saved_dir = infra_mod.resolve_active_memory_dir
    saved_lgb = sys.modules.get("lightgbm")

    def fake_build_training_data(db_path_arg=None):
        import numpy as np

        rng = np.random.default_rng(0)
        X = rng.random((n_impr, n_feat)).astype("float32").tolist()
        y = [2 if i < n_clicks else 1 for i in range(n_impr)]
        qids = [f"q{i % 50}" for i in range(n_impr)]
        return X, y, qids

    sys.modules["lightgbm"] = _make_lightgbm_stub(n_feat)  # type: ignore[assignment]
    infra_mod.resolve_active_memory_dir = lambda: tmp
    cron_mod._build_training_data = fake_build_training_data

    try:
        result = cron_mod.train_ltr_model(dry_run=False)
    finally:
        infra_mod.resolve_active_memory_dir = saved_dir
        if saved_lgb is not None:
            sys.modules["lightgbm"] = saved_lgb
        else:
            sys.modules.pop("lightgbm", None)

    assert result["status"] == "trained"
    assert result["clicks"] == n_clicks
    assert result["total"] == n_impr
    assert isinstance(result["top_features"], list)
    assert len(result["top_features"]) <= n_feat
