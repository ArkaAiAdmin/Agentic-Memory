"""Tests for CTR-driven weight learning (Phase 6).

Covers:
  - Logistic regression fitting
  - AUC computation
  - Per-query-type weight override
  - Cold-start fallback to global prior
"""

from __future__ import annotations

import json
import math
import sqlite3
import time

import pytest


class TestLogisticRegression:
    def test_learning_direction(self):
        from cron.cron_tune_rewrites import _fit_logistic_regression, _sigmoid

        # Larger dataset with clear signal
        X = [[0.1], [0.15], [0.2], [0.25], [0.8], [0.85], [0.9], [0.95]]
        y = [0, 0, 0, 0, 1, 1, 1, 1]
        w = _fit_logistic_regression(X, y, lr=0.5, epochs=500)

        # Should learn positive weight (higher feature = more likely positive)
        assert w[0] > 0

        # Low features should predict < 0.5, high features > 0.5
        low_pred = _sigmoid(w[0] * 0.15)
        high_pred = _sigmoid(w[0] * 0.9)
        assert low_pred < high_pred

    def test_no_signal_random_weights(self):
        from cron.cron_tune_rewrites import _fit_logistic_regression

        X = [[0.5], [0.5], [0.5], [0.5]]
        y = [0, 1, 0, 1]
        w = _fit_logistic_regression(X, y)
        # With no signal, weights should be small
        assert abs(w[0]) < 1.0


class TestAUC:
    def test_perfect_auc(self):
        from cron.cron_tune_rewrites import _compute_auc
        y_true = [0, 0, 1, 1]
        y_scores = [0.1, 0.2, 0.8, 0.9]
        auc = _compute_auc(y_true, y_scores)
        assert auc == 1.0

    def test_random_auc(self):
        from cron.cron_tune_rewrites import _compute_auc
        y_true = [0, 1, 0, 1]
        y_scores = [0.5, 0.5, 0.5, 0.5]
        auc = _compute_auc(y_true, y_scores)
        assert auc == 0.5

    def test_single_class(self):
        from cron.cron_tune_rewrites import _compute_auc
        assert _compute_auc([1, 1, 1], [0.8, 0.9, 0.7]) == 0.5
        assert _compute_auc([0, 0, 0], [0.1, 0.2, 0.3]) == 0.5


class TestQueryTypeWeights:
    def _make_db(self, tmp_path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_query_type_stats (
                query_type   TEXT PRIMARY KEY,
                weights_json TEXT NOT NULL,
                sample_count INTEGER NOT NULL DEFAULT 0,
                updated_at   REAL NOT NULL DEFAULT (unixepoch())
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT,
                fitness_score REAL,
                importance INTEGER,
                pinned INTEGER
            )
            """
        )
        conn.commit()
        return conn

    def test_learned_weights_override_global(self, tmp_path):
        from search.scoring import apply_query_type_weights, _RERANK_WEIGHTS
        conn = self._make_db(tmp_path)

        # Insert learned weights for "code" query type
        learned = {"bm25": 0.3, "fitness": 0.4, "importance": 0.1, "pinned": 0.1, "tag_match": 0.1}
        conn.execute(
            "INSERT INTO memory_query_type_stats (query_type, weights_json, sample_count, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("code", json.dumps(learned), 15, time.time()),
        )
        conn.commit()

        # Override the connection pool to use our test db
        from infra.memory_common import connection_pool
        from infra.memory_common import GLOBAL_MEM_DIR
        old_db = str(GLOBAL_MEM_DIR / "memory.db")

        # Patch connection_pool.get to return our test conn
        original_get = connection_pool.get
        def mock_get(path, **kwargs):
            if path == old_db:
                return conn
            return original_get(path, **kwargs)
        connection_pool.get = mock_get

        try:
            # "code" type should use learned weights
            weights = apply_query_type_weights("code")
            assert weights == learned

            # "general" type should fall back to global prior
            weights = apply_query_type_weights("general")
            assert weights == _RERANK_WEIGHTS
        finally:
            connection_pool.get = original_get
        conn.close()

    def test_low_count_falls_back(self, tmp_path):
        from search.scoring import apply_query_type_weights, _RERANK_WEIGHTS
        conn = self._make_db(tmp_path)

        # Insert weights with low sample count
        learned = {"bm25": 0.3, "fitness": 0.4, "importance": 0.1, "pinned": 0.1, "tag_match": 0.1}
        conn.execute(
            "INSERT INTO memory_query_type_stats (query_type, weights_json, sample_count, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("code", json.dumps(learned), 5, time.time()),  # < 10
        )
        conn.commit()

        from infra.memory_common import connection_pool, GLOBAL_MEM_DIR
        original_get = connection_pool.get
        def mock_get(path, **kwargs):
            if path == str(GLOBAL_MEM_DIR / "memory.db"):
                return conn
            return original_get(path, **kwargs)
        connection_pool.get = mock_get

        try:
            weights = apply_query_type_weights("code")
            assert weights == _RERANK_WEIGHTS  # Falls back to global
        finally:
            connection_pool.get = original_get
        conn.close()


class TestTuneWeights:
    def _make_db_with_interactions(self, tmp_path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_search_interaction (
                id INTEGER PRIMARY KEY,
                query_id TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                action TEXT NOT NULL,
                tenant_id TEXT DEFAULT 'default',
                rank INTEGER,
                ts REAL NOT NULL DEFAULT (unixepoch()),
                UNIQUE (query_id, memory_id, action)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_query_type_stats (
                query_type TEXT PRIMARY KEY,
                weights_json TEXT NOT NULL,
                sample_count INTEGER DEFAULT 0,
                updated_at REAL DEFAULT (unixepoch())
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT,
                fitness_score REAL,
                importance INTEGER,
                pinned INTEGER
            )
        """
        )
        # Insert memories
        for i in range(15):
            conn.execute(
                "INSERT INTO memories (id, content, fitness_score, importance, pinned) VALUES (?, ?, ?, ?, ?)",
                (f"mem{i}", f"content {i}", 0.5 + (i % 3) * 0.1, 3, 0),
            )
        # Insert interactions: 2 query_ids, each with 15 interactions (enough for MIN_INTERACTIONS=10)
        now = time.time()
        for qi in range(2):
            qid = f"query_shared_{qi}"
            for mi in range(15):
                # Vary action: first 8 are clicks, rest are impressions
                action = "click" if mi < 8 else "impression"
                conn.execute(
                    "INSERT OR IGNORE INTO memory_search_interaction (query_id, memory_id, action, rank, ts) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (qid, f"mem{mi % 15}", action, (mi % 15) + 1, now - qi * 100 - mi),
                )
        conn.commit()
        return conn

    def test_tune_writes_weights(self, tmp_path):
        from cron.cron_tune_rewrites import tune_weights
        conn = self._make_db_with_interactions(tmp_path)
        results = tune_weights(conn, days=30)

        # Should have learned weights for some query types
        assert len(results) > 0
        # At least one should be learned (AUC > 0.5)
        learned = [r for r in results.values() if r["status"] == "learned"]
        assert len(learned) > 0
        conn.close()

    def test_dry_run_no_write(self, tmp_path):
        from cron.cron_tune_rewrites import tune_weights
        conn = self._make_db_with_interactions(tmp_path)
        results = tune_weights(conn, days=30, dry_run=True)

        # Table should be empty (dry run)
        count = conn.execute("SELECT COUNT(*) FROM memory_query_type_stats").fetchone()[0]
        assert count == 0
        conn.close()
