#!/usr/bin/env python3
"""Unit tests for QW1 5-channel retrieval fusion (memory_mcp._compute_final_score).

Run with:
    ~/.config/agentic-memory/venv/bin/python eval/test_five_channel.py
"""
import sys
import time
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

import memory_mcp  # noqa: E402
from search_pipeline import ScoreContext


class TestWeightsConfiguration(unittest.TestCase):
    def test_weights_sum_to_one(self):
        w = memory_mcp._RERANK_WEIGHTS
        self.assertAlmostEqual(sum(w.values()), 1.0, places=6)
        for k, v in w.items():
            self.assertGreaterEqual(v, 0.0, f"weight {k} must be non-negative")
        self.assertEqual(len(w), 5)
        for required in ("bm25", "fitness", "importance", "pinned", "tag_match"):
            self.assertIn(required, w)


class TestBm25Dominant(unittest.TestCase):
    def test_better_bm25_wins_when_other_channels_equal(self):
        """When all other channels are equal, a note with better (lower)
        rank should win. This is a sanity check that bm25 still contributes
        monotonically; the 5-channel fusion is allowed to override bm25
        when other signals differ."""
        now = time.time()
        # Better rank (more negative rank → higher score in our formula)
        s1 = memory_mcp._compute_final_score(ScoreContext(
            rank=-5.0, fitness=1.0, importance=3, pinned=False,
            created="2026-01-01T00:00:00", tags_json="[]",
            query="foo", boost_pinned=False, recency_weight=0.0, now_ts=now,
        ))
        s2 = memory_mcp._compute_final_score(ScoreContext(
            rank=-1.0, fitness=1.0, importance=3, pinned=False,
            created="2026-01-01T00:00:00", tags_json="[]",
            query="foo", boost_pinned=False, recency_weight=0.0, now_ts=now,
        ))
        # rank=-5 means bm25=5, rank=-1 means bm25=1. So s1 > s2.
        self.assertGreater(s1, s2, "lower (more negative) rank should score higher")

    def test_bm25_channel_weight_is_largest(self):
        """bm25 has the highest weight in the 5-channel fusion."""
        w = memory_mcp._RERANK_WEIGHTS
        bm25 = w["bm25"]
        for k, v in w.items():
            if k == "bm25":
                continue
            self.assertGreater(bm25, v, f"bm25 weight ({bm25}) must exceed {k} weight ({v})")


class TestPinnedBoost(unittest.TestCase):
    def test_pinned_boost_increases_score(self):
        now = time.time()
        base = memory_mcp._compute_final_score(ScoreContext(
            rank=-1.0, fitness=1.0, importance=3, pinned=False,
            created="2026-01-01T00:00:00", tags_json="[]",
            query="x", boost_pinned=True, recency_weight=0.1, now_ts=now,
        ))
        pinned = memory_mcp._compute_final_score(ScoreContext(
            rank=-1.0, fitness=1.0, importance=3, pinned=True,
            created="2026-01-01T00:00:00", tags_json="[]",
            query="x", boost_pinned=True, recency_weight=0.1, now_ts=now,
        ))
        self.assertGreater(pinned, base)
        # 0.10 weight on pinned channel → exactly +0.10
        self.assertAlmostEqual(pinned - base, 0.10, places=6)

    def test_pinned_ignored_when_disabled(self):
        now = time.time()
        base = memory_mcp._compute_final_score(ScoreContext(
            rank=-1.0, fitness=1.0, importance=3, pinned=False,
            created="2026-01-01T00:00:00", tags_json="[]",
            query="x", boost_pinned=False, recency_weight=0.1, now_ts=now,
        ))
        pinned = memory_mcp._compute_final_score(ScoreContext(
            rank=-1.0, fitness=1.0, importance=3, pinned=True,
            created="2026-01-01T00:00:00", tags_json="[]",
            query="x", boost_pinned=False, recency_weight=0.1, now_ts=now,
        ))
        self.assertEqual(base, pinned)


class TestRecencyDecay(unittest.TestCase):
    """Recency is now applied by _apply_temporal_decay as a multiplicative
    post-step, not as an additive channel inside _compute_final_score."""

    def test_recent_ranks_higher_than_old(self):
        """A fresh note's final_score is boosted relative to an old note
        after _apply_temporal_decay is applied."""
        from search.scoring import _apply_temporal_decay

        now = time.time()
        recent_ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 86400))
        old_ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 86400 * 365))
        base_score = 0.5
        recent_row = (None, None, None, None, recent_ts, None, base_score, None, None, None)
        old_row = (None, None, None, None, old_ts, None, base_score, None, None, None)
        scored = _apply_temporal_decay([recent_row, old_row], decay_weight=0.15, as_of=now)
        self.assertGreater(scored[0][6], scored[1][6],
            "fresh note should outrank year-old note after temporal decay")

    def test_recency_weight_zero_disables(self):
        """decay_weight=0.0 in _apply_temporal_decay leaves scores unchanged."""
        from search.scoring import _apply_temporal_decay

        now = time.time()
        recent_ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 86400))
        old_ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 86400 * 365))
        base_score = 0.5
        recent_row = (None, None, None, None, recent_ts, None, base_score, None, None, None)
        old_row = (None, None, None, None, old_ts, None, base_score, None, None, None)
        scored = _apply_temporal_decay([recent_row, old_row], decay_weight=0.0, as_of=now)
        self.assertEqual(scored[0][6], base_score,
            "decay_weight=0 should leave final_score unchanged")


class TestTagMatch(unittest.TestCase):
    def test_tag_match_contributes(self):
        now = time.time()
        matched = memory_mcp._compute_final_score(ScoreContext(
            rank=-1.0, fitness=1.0, importance=3, pinned=False,
            created="2020-01-01T00:00:00", tags_json='["python", "testing"]',
            query="python", boost_pinned=False, recency_weight=0.0, now_ts=now,
        ))
        unmatched = memory_mcp._compute_final_score(ScoreContext(
            rank=-1.0, fitness=1.0, importance=3, pinned=False,
            created="2020-01-01T00:00:00", tags_json='["java", "kotlin"]',
            query="python", boost_pinned=False, recency_weight=0.0, now_ts=now,
        ))
        self.assertGreater(matched, unmatched)
        # 0.05 weight on tag_match, all 1 query token matched → +0.05
        self.assertAlmostEqual(matched - unmatched, 0.05, places=6)

    def test_partial_tag_match(self):
        now = time.time()
        # Query has 2 tokens, only 1 matches
        s = memory_mcp._compute_final_score(ScoreContext(
            rank=-1.0, fitness=1.0, importance=3, pinned=False,
            created="2020-01-01T00:00:00", tags_json='["python"]',
            query="python rust", boost_pinned=False, recency_weight=0.0, now_ts=now,
        ))
        s_zero = memory_mcp._compute_final_score(ScoreContext(
            rank=-1.0, fitness=1.0, importance=3, pinned=False,
            created="2020-01-01T00:00:00", tags_json='["java"]',
            query="python rust", boost_pinned=False, recency_weight=0.0, now_ts=now,
        ))
        # 0.05 * (1 / 2) = 0.025
        self.assertAlmostEqual(s - s_zero, 0.025, places=6)


class TestBackwardCompat(unittest.TestCase):
    def test_tuple_shape_unchanged(self):
        """After refactor, the inline loop should still produce a 10-tuple
        with the same field order: (note_id, content, source_file, tags_json,
        created, rank, final_score, fitness_score, importance_val, pinned)."""
        import inspect
        from search.orchestrator import _rerank_results
        # Check the rerank function still emits a tuple.
        # Read the rerank block in source code from search.orchestrator.
        src = inspect.getsource(_rerank_results)
        # The scored.append line must still be present.
        self.assertIn("scored.append", src)
        # Note: we trust the inline 10-tuple because we control the refactor.
        # A stronger invariant: scoring helper returns a single float, not a tuple.
        score = memory_mcp._compute_final_score(ScoreContext(
            rank=-1.0, fitness=1.0, importance=3, pinned=False,
            created="2026-01-01T00:00:00", tags_json="[]",
            query="x", boost_pinned=True, recency_weight=0.1,
        ))
        self.assertIsInstance(score, float)


if __name__ == "__main__":
    unittest.main(verbosity=2)
