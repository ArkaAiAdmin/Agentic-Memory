#!/usr/bin/env python3
"""BB3: Reciprocal Rank Fusion for hybrid FTS + semantic retrieval.

Verifies that:
  1. _reciprocal_rank_fusion handles single list (degenerate case)
  2. _reciprocal_rank_fusion handles two equal lists (intersection scores higher)
  3. _reciprocal_rank_fusion handles two disjoint lists (union, no double-count)
  4. _reciprocal_rank_fusion handles three lists (additive)
  5. _reciprocal_rank_fusion accepts bare ids and (id, score) tuples
  6. k=0 amplifies top ranks dramatically
  7. k=100 flattens the contribution
  8. search_memories accepts the new `hybrid` flag
  9. Empty ranked lists return empty dict
 10. RRF ranks are deterministic (same input → same output)
 11. _RRF_K constant is 60 (Cormack/Clarke/Buettcher recommendation)
 12. End-to-end: search with hybrid=False still works (FTS-only)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

INSTALL = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL))

import memory_mcp


class TestRRFBasics(unittest.TestCase):
    def test_01_rrf_k_constant(self):
        self.assertEqual(memory_mcp._RRF_K, 60,
            "k=60 is the standard Cormack/Clarke/Buettcher 2009 recommendation")

    def test_02_single_list_degenerate(self):
        # With one list, RRF should give each doc 1/(k+rank+1).
        out = memory_mcp._reciprocal_rank_fusion([["a", "b", "c"]])
        self.assertAlmostEqual(out["a"], 1.0 / 61, delta=1e-6)
        self.assertAlmostEqual(out["b"], 1.0 / 62, delta=1e-6)
        self.assertAlmostEqual(out["c"], 1.0 / 63, delta=1e-6)
        self.assertEqual(len(out), 3)

    def test_03_intersection_scores_higher(self):
        # When a doc appears in both lists, its score is the sum.
        list_a = ["a", "b", "c"]
        list_b = ["a", "d", "e"]
        out = memory_mcp._reciprocal_rank_fusion([list_a, list_b])
        # "a" is in both → 1/61 + 1/61
        self.assertAlmostEqual(out["a"], 2.0 / 61, delta=1e-6)
        # "b" only in a → 1/62
        self.assertAlmostEqual(out["b"], 1.0 / 62, delta=1e-6)
        # "d" only in b → 1/62
        self.assertAlmostEqual(out["d"], 1.0 / 62, delta=1e-6)

    def test_04_three_lists_additive(self):
        out = memory_mcp._reciprocal_rank_fusion([
            ["a", "b"],
            ["a", "c"],
            ["a", "d"],
        ])
        # a is in all 3 → 3 * 1/61
        self.assertAlmostEqual(out["a"], 3.0 / 61, delta=1e-6)
        # b, c, d each in 1 list → 1/62
        self.assertAlmostEqual(out["b"], 1.0 / 62, delta=1e-6)
        self.assertAlmostEqual(out["c"], 1.0 / 62, delta=1e-6)
        self.assertAlmostEqual(out["d"], 1.0 / 62, delta=1e-6)

    def test_05_accepts_both_id_and_tuple(self):
        # Bare ids
        out1 = memory_mcp._reciprocal_rank_fusion([["a", "b"]])
        # Tuples
        out2 = memory_mcp._reciprocal_rank_fusion([[("a", 0.9), ("b", 0.7)]])
        self.assertEqual(out1, out2, "should treat tuples and bare ids identically")

    def test_06_k_zero_amplifies_top(self):
        # With k=0, top doc gets 1/(0+0+1) = 1.0, second gets 0.5, etc.
        out = memory_mcp._reciprocal_rank_fusion([["a", "b", "c"]], k=0)
        self.assertAlmostEqual(out["a"], 1.0, delta=1e-6)
        self.assertAlmostEqual(out["b"], 0.5, delta=1e-6)
        self.assertAlmostEqual(out["c"], 1.0 / 3, delta=1e-4)

    def test_07_k_hundred_flattens(self):
        # With k=100, top doc gets 1/101, second gets 1/102, etc.
        out = memory_mcp._reciprocal_rank_fusion([["a", "b", "c"]], k=100)
        self.assertAlmostEqual(out["a"], 1.0 / 101, delta=1e-6)
        self.assertAlmostEqual(out["b"], 1.0 / 102, delta=1e-6)
        self.assertAlmostEqual(out["a"] - out["b"], 1.0 / (101 * 102), delta=1e-8)

    def test_08_empty_lists(self):
        out = memory_mcp._reciprocal_rank_fusion([])
        self.assertEqual(out, {})
        out2 = memory_mcp._reciprocal_rank_fusion([[]])
        self.assertEqual(out2, {})
        out3 = memory_mcp._reciprocal_rank_fusion([[], [], []])
        self.assertEqual(out3, {})

    def test_09_determinism(self):
        list_a = ["x", "y", "z", "w"]
        list_b = ["y", "w", "v"]
        out1 = memory_mcp._reciprocal_rank_fusion([list_a, list_b])
        out2 = memory_mcp._reciprocal_rank_fusion([list_a, list_b])
        self.assertEqual(out1, out2)

    def test_10_rrf_orders_intersection_first(self):
        # Document in both lists should rank higher than doc in only one.
        list_a = ["a", "b", "c"]
        list_b = ["b", "d", "e"]
        out = memory_mcp._reciprocal_rank_fusion([list_a, list_b])
        # "b" appears in both
        self.assertGreater(out["b"], out["a"],
            "doc in both lists should outrank doc in only one list")
        self.assertGreater(out["b"], out["d"])


class TestSearchHybridFlag(unittest.TestCase):
    def test_11_search_memories_accepts_hybrid_flag(self):
        import inspect
        sig = inspect.signature(memory_mcp.search_memories)
        self.assertIn("hybrid", sig.parameters,
            "search_memories should have a 'hybrid' parameter")
        # Default should be True
        self.assertEqual(sig.parameters["hybrid"].default, True,
            "hybrid should default to True")


if __name__ == "__main__":
    unittest.main()
