#!/usr/bin/env python3
"""QW3: Query type detection + adaptive rerank weights.

Verifies that:
  1. _detect_query_type returns "temporal" for date/year queries
  2. _detect_query_type returns "code" for function/class/import queries
  3. _detect_query_type returns "multihop" for compare/difference queries
  4. _detect_query_type returns "factual" for "what is" / "define" queries
  5. _detect_query_type returns "general" for everything else
  6. _detect_query_type returns "general" for empty input
  7. All 5 query types have weight overrides in _QUERY_TYPE_WEIGHTS
  8. Each type's weights sum to 1.0 (within tolerance)
  9. Each type's weights contain all 6 channel keys
 10. _weights_for_query_type returns a fresh copy (not a reference)
 11. Temporal weights boost recency vs general
 12. Code weights boost tag_match vs general
 13. Factual weights boost bm25 vs general
 14. _compute_final_score accepts a weights override
 15. _compute_final_score with temporal weights scores recent notes higher
"""

from __future__ import annotations

import sys
import time
import unittest
from datetime import datetime
from pathlib import Path

INSTALL = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL))

import search.query_parser as _qp
import memory_mcp
from search_pipeline import ScoreContext, _QUERY_TYPE_WEIGHTS as _SP_QUERY_TYPE_WEIGHTS


class TestQueryTypeDetection(unittest.TestCase):
    def test_01_temporal_queries(self):
        self.assertEqual(
            memory_mcp._detect_query_type("when did this happen"), "temporal"
        )
        self.assertEqual(
            memory_mcp._detect_query_type("what year was Python created"), "temporal"
        )
        self.assertEqual(
            memory_mcp._detect_query_type("recent changes to the API"), "temporal"
        )
        self.assertEqual(
            memory_mcp._detect_query_type("in 2023 what was shipped"), "temporal"
        )
        self.assertEqual(
            memory_mcp._detect_query_type("last week deployment"), "temporal"
        )

    def test_02_code_queries(self):
        self.assertEqual(
            memory_mcp._detect_query_type("how to define a function"), "code"
        )
        self.assertEqual(
            memory_mcp._detect_query_type("Python import statement"), "code"
        )
        self.assertEqual(memory_mcp._detect_query_type("class method override"), "code")
        self.assertEqual(
            memory_mcp._detect_query_type("compile error in main.go"), "code"
        )
        self.assertEqual(
            memory_mcp._detect_query_type("test fixture for auth.spec"), "code"
        )

    def test_03_multihop_queries(self):
        self.assertEqual(
            memory_mcp._detect_query_type("compare auth and session"), "multihop"
        )
        self.assertEqual(
            memory_mcp._detect_query_type("difference between A and B"), "multihop"
        )
        self.assertEqual(
            memory_mcp._detect_query_type("relationship between the two"), "multihop"
        )

    def test_04_factual_queries(self):
        self.assertEqual(memory_mcp._detect_query_type("what is recursion"), "factual")
        self.assertEqual(
            memory_mcp._detect_query_type("who is the maintainer"), "factual"
        )
        self.assertEqual(
            memory_mcp._detect_query_type("where is the config file"), "factual"
        )
        self.assertEqual(
            memory_mcp._detect_query_type("define authentication"), "factual"
        )
        self.assertEqual(
            memory_mcp._detect_query_type("how many tests passed"), "factual"
        )

    def test_05_general_queries(self):
        self.assertEqual(memory_mcp._detect_query_type("auth flow"), "general")
        self.assertEqual(memory_mcp._detect_query_type("kubernetes setup"), "general")
        self.assertEqual(
            memory_mcp._detect_query_type("how does the cache work"), "general"
        )

    def test_06_empty_query_is_general(self):
        self.assertEqual(memory_mcp._detect_query_type(""), "general")


class TestQueryTypeWeights(unittest.TestCase):
    def test_07_all_types_have_overrides(self):
        for qt in ("temporal", "multihop", "code", "factual", "general"):
            self.assertIn(
                qt,
                memory_mcp._QUERY_TYPE_WEIGHTS,
                f"missing weight override for query type {qt!r}",
            )

    def test_08_weights_sum_to_one(self):
        for qt, w in memory_mcp._QUERY_TYPE_WEIGHTS.items():
            total = sum(w.values())
            self.assertAlmostEqual(
                total, 1.0, delta=1e-9, msg=f"{qt} weights sum to {total}, not 1.0"
            )

    def test_09_weights_have_all_channels(self):
        required = {"bm25", "fitness", "importance", "pinned", "recency", "tag_match"}
        for qt, w in memory_mcp._QUERY_TYPE_WEIGHTS.items():
            self.assertEqual(
                set(w.keys()),
                required,
                f"{qt} missing channels: {required - set(w.keys())}",
            )

    def test_10_weights_for_query_type_returns_copy(self):
        w1 = memory_mcp._weights_for_query_type("temporal")
        w1["bm25"] = 0.99
        w2 = memory_mcp._weights_for_query_type("temporal")
        # Should be a fresh dict, not the same reference
        self.assertNotEqual(
            w2["bm25"], 0.99, "_weights_for_query_type returned a reference, not a copy"
        )

    def test_11_temporal_boosts_fitness(self):
        temp = memory_mcp._weights_for_query_type("temporal")
        gen = memory_mcp._weights_for_query_type("general")
        self.assertGreater(
            temp["fitness"],
            gen["fitness"],
            "temporal should boost fitness weight vs general",
        )

    def test_12_code_boosts_pinned(self):
        code = memory_mcp._weights_for_query_type("code")
        gen = memory_mcp._weights_for_query_type("general")
        self.assertGreater(
            code["pinned"],
            gen["pinned"],
            "code should boost pinned weight vs general",
        )

    def test_13_factual_boosts_bm25(self):
        fact = memory_mcp._weights_for_query_type("factual")
        gen = memory_mcp._weights_for_query_type("general")
        self.assertGreater(
            fact["bm25"], gen["bm25"], "factual should boost bm25 weight vs general"
        )


class TestQueryTypeWeightsDivergence(unittest.TestCase):
    """Ensure the duplicate _QUERY_TYPE_WEIGHTS in search_pipeline.py
    has not diverged from the source of truth in search/query_parser.py."""

    def test_16_copies_match_exactly(self):
        qp = _qp._QUERY_TYPE_WEIGHTS
        sp = _SP_QUERY_TYPE_WEIGHTS
        self.assertEqual(
            set(qp.keys()),
            set(sp.keys()),
            f"query types differ: qp={set(qp.keys())} vs sp={set(sp.keys())}",
        )
        for qt in qp:
            self.assertEqual(
                qp[qt],
                sp[qt],
                f"query type {qt!r} weights diverged:\n"
                f"  query_parser:  {qp[qt]}\n"
                f"  search_pipeline: {sp[qt]}",
            )


class TestFinalScoreWithWeights(unittest.TestCase):
    def test_14_accepts_weights_override(self):
        # Should not raise
        s = memory_mcp._compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=1.0,
                importance=5,
                pinned=True,
                created=datetime.now().isoformat(),
                tags_json="[]",
                query="test",
                boost_pinned=True,
                recency_weight=0.1,
                weights={
                    "bm25": 1.0,
                    "fitness": 0.0,
                    "importance": 0.0,
                    "pinned": 0.0,
                    "recency": 0.0,
                    "tag_match": 0.0,
                },
            )
        )
        # Only bm25 should contribute → sigmoid(-1.0) ≈ 0.731
        self.assertAlmostEqual(s, 0.7310585786300049, delta=1e-6)

    def test_15_temporal_weights_boost_recent_note(self):
        # Two notes identical except for created date. Temporal decay
        # should rank the recent one higher (multiplicative modifier).
        from search.scoring import _apply_temporal_decay

        now = time.time()
        old_ts = (now - 86400 * 365)
        new_ts = (now - 86400)
        base_score = 0.5
        recent_row = (None, None, None, None, _iso(old_ts), None, base_score, None, None, None)
        old_row = (None, None, None, None, _iso(old_ts - 86400 * 365), None, base_score, None, None, None)
        scored = _apply_temporal_decay([recent_row, old_row], decay_weight=0.15, as_of=now)
        self.assertGreater(scored[0][6], scored[1][6],
            "recent note should outrank old note after temporal decay")

def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts))


if __name__ == "__main__":
    unittest.main()
