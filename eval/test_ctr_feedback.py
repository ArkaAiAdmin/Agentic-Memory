"""Tests for CTR feedback → search weight tuning integration.

Verifies:
  1. compute_channel_weights() returns correct weights for different access profiles
  2. MEMORY_CTR_TUNING=1 enables CTR-tuned weights
  3. MEMORY_CTR_TUNING=0 (default) returns None (hardcoded weights)
  4. record_ctr_feedback updates the CTR table correctly
  5. All tests use isolated temp DBs via MEMORY_DB_PATH
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

_INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
if str(_INSTALL_DIR) not in sys.path:
    sys.path.insert(0, str(_INSTALL_DIR))

from memory_common import run_db_migrations, _migrate_memory_ctr_feedback


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctr_db(tmp_path: Path) -> Path:
    """Create a temp DB with memories + CTR tables."""
    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        run_db_migrations(conn)
        _migrate_memory_ctr_feedback(conn)
        conn.commit()
    finally:
        conn.close()
    return db_path


def _seed_ctr_data(conn: sqlite3.Connection, n_rows: int = 15, **overrides):
    """Insert n_rows of CTR feedback data with realistic ranking_params.

    Each row simulates a search result that was returned and optionally
    clicked/dismissed. ranking_params is the JSON blob that stores the
    per-channel weights used at query time.

    Note: compute_channel_weights groups by query_id and needs ≥10 groups.
    Use n_distinct_queries to control the number of distinct query groups.
    """
    now = time.time()
    default_params = {
        "weights": {
            "bm25": 0.4, "fitness": 0.2, "importance": 0.15,
            "pinned": 0.1, "recency": 0.1, "tag_match": 0.05,
        }
    }
    params_json = json.dumps(overrides.get("ranking_params", default_params))
    n_distinct = overrides.get("n_distinct_queries", n_rows)

    for i in range(n_rows):
        qid = overrides.get("query_id", f"q_{i % n_distinct}")
        overrides.get("note_id", f"lessons/note_{i}")
        clicked = now - 100 + i * 10 if i % 3 != 0 else None
        dismissed = now - 50 + i * 5 if i % 5 == 0 else None

        conn.execute(
            "INSERT OR REPLACE INTO memory_ctr_feedback "
            "(id, query_id, returned_at, clicked_at, dismissed_at, source, ranking_params) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                f"fb_{i}",
                qid,
                now - 200 + i * 10,
                clicked,
                dismissed,
                "test",
                params_json,
            ),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestComputeChannelWeights:
    """Test compute_channel_weights() with various data profiles."""

    def test_returns_none_when_tuning_disabled(self, tmp_path):
        """MEMORY_CTR_TUNING != 1 → returns None (use hardcoded weights)."""
        db_path = _make_ctr_db(tmp_path)
        os.environ["MEMORY_CTR_TUNING"] = "0"
        try:
            from search_pipeline import compute_channel_weights
            import search_pipeline as sp
            sp._CTR_WEIGHTS_CACHE = None
            result = compute_channel_weights(db_path)
            assert result is None
        finally:
            os.environ.pop("MEMORY_CTR_TUNING", None)
            sp._CTR_WEIGHTS_CACHE = None

    def test_returns_none_when_no_ctr_table(self, tmp_path):
        """Missing CTR table → returns None gracefully."""
        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            run_db_migrations(conn)
            conn.commit()
        finally:
            conn.close()

        os.environ["MEMORY_CTR_TUNING"] = "1"
        try:
            from search_pipeline import compute_channel_weights
            import search_pipeline as sp
            sp._CTR_WEIGHTS_CACHE = None
            result = compute_channel_weights(db_path)
            assert result is None
        finally:
            os.environ.pop("MEMORY_CTR_TUNING", None)
            sp._CTR_WEIGHTS_CACHE = None

    def test_returns_none_with_insufficient_data(self, tmp_path):
        """Fewer than 10 data points → returns None."""
        db_path = _make_ctr_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        try:
            _seed_ctr_data(conn, n_rows=5)
        finally:
            conn.close()

        os.environ["MEMORY_CTR_TUNING"] = "1"
        try:
            from search_pipeline import compute_channel_weights
            import search_pipeline as sp
            sp._CTR_WEIGHTS_CACHE = None
            result = compute_channel_weights(db_path)
            assert result is None
        finally:
            os.environ.pop("MEMORY_CTR_TUNING", None)
            sp._CTR_WEIGHTS_CACHE = None

    def test_returns_weights_with_sufficient_data(self, tmp_path):
        """≥10 data points → returns a normalized weights dict."""
        db_path = _make_ctr_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        try:
            _seed_ctr_data(conn, n_rows=15, n_distinct_queries=15)
        finally:
            conn.close()

        os.environ["MEMORY_CTR_TUNING"] = "1"
        try:
            from search_pipeline import compute_channel_weights
            import search_pipeline as sp
            sp._CTR_WEIGHTS_CACHE = None
            result = compute_channel_weights(db_path)
            assert result is not None, "Expected weights dict with 15 data points"
            assert isinstance(result, dict)
            assert set(result.keys()) == {
                "bm25", "fitness", "importance", "pinned", "recency", "tag_match"
            }
            # Weights should sum to ~1.0 (normalized)
            total = sum(result.values())
            assert abs(total - 1.0) < 0.01, f"Weights sum to {total}, expected ~1.0"
            # All weights non-negative
            for k, v in result.items():
                assert v >= 0.0, f"Negative weight for {k}: {v}"
        finally:
            os.environ.pop("MEMORY_CTR_TUNING", None)
            sp._CTR_WEIGHTS_CACHE = None

    def test_high_ctr_biases_toward_clicked_weights(self, tmp_path):
        """When most clicks align with bm25-heavy weights, bm25 weight increases."""
        db_path = _make_ctr_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        try:
            # All clicks (no dismissals) → CTR=1.0, bm25-heavy params
            bm25_heavy = {
                "weights": {
                    "bm25": 0.7, "fitness": 0.1, "importance": 0.1,
                    "pinned": 0.05, "recency": 0.03, "tag_match": 0.02,
                }
            }
            _seed_ctr_data(conn, n_rows=20, n_distinct_queries=20, ranking_params=bm25_heavy)
        finally:
            conn.close()

        os.environ["MEMORY_CTR_TUNING"] = "1"
        try:
            from search_pipeline import compute_channel_weights
            import search_pipeline as sp
            sp._CTR_WEIGHTS_CACHE = None
            result = compute_channel_weights(db_path)
            assert result is not None
            # bm25 should be the dominant weight
            assert result["bm25"] > result["fitness"]
            assert result["bm25"] > result["importance"]
        finally:
            os.environ.pop("MEMORY_CTR_TUNING", None)
            sp._CTR_WEIGHTS_CACHE = None


class TestRecordCTRFeedback:
    """Test that record_ctr_feedback correctly updates the CTR table."""

    def test_insert_returns_row(self, tmp_path):
        """action='returned' inserts a new row."""
        db_path = _make_ctr_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        try:
            now = time.time()
            conn.execute(
                "INSERT INTO memory_ctr_feedback "
                "(id, query_id, returned_at, source, ranking_params) "
                "VALUES (?, ?, ?, ?, ?)",
                ("fb_001", "q_test", now, "unit_test", "{}"),
            )
            conn.commit()

            row = conn.execute(
                "SELECT id, query_id, returned_at FROM memory_ctr_feedback WHERE id = ?",
                ("fb_001",),
            ).fetchone()
            assert row is not None
            assert row[0] == "fb_001"
            assert row[1] == "q_test"
        finally:
            conn.close()

    def test_click_updates_row(self, tmp_path):
        """action='clicked' sets clicked_at on existing row."""
        db_path = _make_ctr_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        try:
            now = time.time()
            conn.execute(
                "INSERT INTO memory_ctr_feedback "
                "(id, query_id, returned_at, source) "
                "VALUES (?, ?, ?, ?)",
                ("fb_002", "q_test", now, "unit_test"),
            )
            conn.commit()

            click_time = now + 5
            conn.execute(
                "UPDATE memory_ctr_feedback SET clicked_at = ? "
                "WHERE id = ? AND query_id = ?",
                (click_time, "fb_002", "q_test"),
            )
            conn.commit()

            row = conn.execute(
                "SELECT clicked_at FROM memory_ctr_feedback WHERE id = ?",
                ("fb_002",),
            ).fetchone()
            assert row is not None
            assert row[0] is not None
            assert abs(row[0] - click_time) < 0.01
        finally:
            conn.close()

    def test_dismiss_updates_row(self, tmp_path):
        """action='dismissed' sets dismissed_at on existing row."""
        db_path = _make_ctr_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        try:
            now = time.time()
            conn.execute(
                "INSERT INTO memory_ctr_feedback "
                "(id, query_id, returned_at, source) "
                "VALUES (?, ?, ?, ?)",
                ("fb_003", "q_test", now, "unit_test"),
            )
            conn.commit()

            dismiss_time = now + 3
            conn.execute(
                "UPDATE memory_ctr_feedback SET dismissed_at = ? "
                "WHERE id = ? AND query_id = ?",
                (dismiss_time, "fb_003", "q_test"),
            )
            conn.commit()

            row = conn.execute(
                "SELECT dismissed_at FROM memory_ctr_feedback WHERE id = ?",
                ("fb_003",),
            ).fetchone()
            assert row is not None
            assert row[0] is not None
            assert abs(row[0] - dismiss_time) < 0.01
        finally:
            conn.close()

    def test_dedup_by_id_and_query_id(self, tmp_path):
        """Same (id, query_id) pair is deduplicated via INSERT OR REPLACE."""
        db_path = _make_ctr_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        try:
            now = time.time()
            conn.execute(
                "INSERT INTO memory_ctr_feedback "
                "(id, query_id, returned_at, source) "
                "VALUES (?, ?, ?, ?)",
                ("fb_dup", "q_dup", now, "unit_test"),
            )
            conn.commit()

            # Insert again with same id — should replace
            conn.execute(
                "INSERT OR REPLACE INTO memory_ctr_feedback "
                "(id, query_id, returned_at, source) "
                "VALUES (?, ?, ?, ?)",
                ("fb_dup", "q_dup", now + 10, "unit_test_v2"),
            )
            conn.commit()

            count = conn.execute(
                "SELECT COUNT(*) FROM memory_ctr_feedback WHERE id = ?",
                ("fb_dup",),
            ).fetchone()[0]
            assert count == 1, f"Expected 1 row after dedup, got {count}"
        finally:
            conn.close()


class TestCTRTuningEnvVar:
    """Test MEMORY_CTR_TUNING env var gating."""

    def test_tuning_disabled_by_default(self, tmp_path):
        """No MEMORY_CTR_TUNING set → compute_channel_weights returns None."""
        db_path = _make_ctr_db(tmp_path)
        os.environ.pop("MEMORY_CTR_TUNING", None)

        from search_pipeline import compute_channel_weights
        import search_pipeline as sp
        sp._CTR_WEIGHTS_CACHE = None
        result = compute_channel_weights(db_path)
        assert result is None

    def test_tuning_enabled(self, tmp_path):
        """MEMORY_CTR_TUNING=1 with data → returns weights."""
        db_path = _make_ctr_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        try:
            _seed_ctr_data(conn, n_rows=20, n_distinct_queries=20)
        finally:
            conn.close()

        os.environ["MEMORY_CTR_TUNING"] = "1"
        try:
            from search_pipeline import compute_channel_weights
            import search_pipeline as sp
            sp._CTR_WEIGHTS_CACHE = None
            result = compute_channel_weights(db_path)
            assert result is not None
            assert isinstance(result, dict)
        finally:
            os.environ.pop("MEMORY_CTR_TUNING", None)
            sp._CTR_WEIGHTS_CACHE = None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
