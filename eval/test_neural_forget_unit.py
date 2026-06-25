#!/usr/bin/env python3
"""Unit tests for neural_forget.py.

The pure, model-independent surface:
  * ``surprise_score`` (Jaccard distance, 0=identical, 1=disjoint)
  * ``_sigmoid`` (logistic, overflow-safe)
  * ``compute_retention_rate`` (the main scoring formula)
  * ``is_available`` (always True — no model)
  * ``compute_query_surprise`` (without DB → just query surprise)
  * Edge cases: empty content, zero recency, maxed importance

The DB-dependent functions (``compute_forgetting_rate``,
``batch_update_retention``) are exercised by integration tests
because they need a real ``memories`` table; this file only
covers the pure surface.
"""

import math
import sys
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))
sys.path.insert(0, str(INSTALL_DIR / "eval"))


class TestSurpriseScore(unittest.TestCase):
    def test_identical_content_zero_distance(self):
        from neural_forget import surprise_score

        s = surprise_score("the quick brown fox", "the quick brown fox")
        self.assertEqual(s, 0.0)

    def test_disjoint_content_high_distance(self):
        from neural_forget import surprise_score

        s = surprise_score("apple banana", "orange grape kiwi")
        self.assertEqual(s, 1.0)

    def test_partial_overlap_between_zero_and_one(self):
        from neural_forget import surprise_score

        s = surprise_score("the quick brown fox", "the lazy brown dog")
        # Some words overlap ("the", "brown"), some don't.
        self.assertGreater(s, 0.0)
        self.assertLess(s, 1.0)

    def test_empty_content_returns_neutral(self):
        from neural_forget import surprise_score

        self.assertEqual(surprise_score("", "anything"), 0.5)
        self.assertEqual(surprise_score("anything", ""), 0.5)

    def test_case_insensitive(self):
        from neural_forget import surprise_score

        a = surprise_score("Python is great", "PYTHON IS GREAT")
        self.assertEqual(a, 0.0)


class TestSigmoid(unittest.TestCase):
    def test_zero_returns_half(self):
        from neural_forget import _sigmoid

        self.assertAlmostEqual(_sigmoid(0.0), 0.5, places=6)

    def test_large_positive_returns_one(self):
        from neural_forget import _sigmoid

        self.assertAlmostEqual(_sigmoid(1000.0), 1.0, places=6)

    def test_large_negative_returns_zero(self):
        from neural_forget import _sigmoid

        self.assertAlmostEqual(_sigmoid(-1000.0), 0.0, places=6)

    def test_overflow_returns_saturating_value(self):
        """Must not raise OverflowError; returns the saturating limit."""
        from neural_forget import _sigmoid

        # 10000.0 would overflow math.exp on the naive path.
        self.assertIn(_sigmoid(10000.0), (0.0, 1.0))
        self.assertIn(_sigmoid(-10000.0), (0.0, 1.0))


class TestComputeRetentionRate(unittest.TestCase):
    def test_output_in_unit_interval(self):
        from neural_forget import compute_retention_rate

        for access in (0, 1, 5, 50):
            for recency in (0.0, 1.0, 100.0, 1000.0):
                r = compute_retention_rate(
                    content="x",
                    access_count=access,
                    recency_days=recency,
                    fitness=0.5,
                    importance=3,
                )
                self.assertGreaterEqual(r, 0.0)
                self.assertLessEqual(r, 1.0)

    def test_high_importance_increases_retention(self):
        from neural_forget import compute_retention_rate

        low = compute_retention_rate(
            content="x", access_count=5, recency_days=10, fitness=0.5, importance=1
        )
        high = compute_retention_rate(
            content="x", access_count=5, recency_days=10, fitness=0.5, importance=5
        )
        self.assertGreater(high, low)

    def test_high_recency_decreases_retention(self):
        from neural_forget import compute_retention_rate

        fresh = compute_retention_rate(
            content="x", access_count=5, recency_days=0.0, fitness=0.5, importance=3
        )
        stale = compute_retention_rate(
            content="x", access_count=5, recency_days=300.0, fitness=0.5, importance=3
        )
        self.assertGreater(fresh, stale)

    def test_high_access_increases_retention(self):
        from neural_forget import compute_retention_rate

        never = compute_retention_rate(
            content="x", access_count=0, recency_days=10, fitness=0.5, importance=3
        )
        often = compute_retention_rate(
            content="x", access_count=50, recency_days=10, fitness=0.5, importance=3
        )
        self.assertGreater(often, never)

    def test_recency_saturates_at_cap(self):
        """Days past _RECENCY_CAP should not push retention below
        the saturated recency value."""
        from neural_forget import compute_retention_rate

        at_cap = compute_retention_rate(
            content="x", access_count=5, recency_days=365.0, fitness=0.5, importance=3
        )
        past_cap = compute_retention_rate(
            content="x", access_count=5, recency_days=5000.0, fitness=0.5, importance=3
        )
        # Should be exactly equal (cap saturates).
        self.assertAlmostEqual(at_cap, past_cap, places=6)


class TestIsAvailable(unittest.TestCase):
    def test_always_true(self):
        from neural_forget import is_available

        self.assertTrue(is_available())


class TestComputeQuerySurpriseNoDB(unittest.TestCase):
    def test_no_db_returns_just_query_surprise(self):
        from neural_forget import compute_query_surprise

        s = compute_query_surprise("apple banana", "apple banana")
        # Identical query → 0 distance.
        self.assertEqual(s, 0.0)

    def test_different_query_returns_positive_surprise(self):
        from neural_forget import compute_query_surprise

        s = compute_query_surprise("apple banana", "cherry date")
        # Disjoint → 1.0.
        self.assertEqual(s, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
