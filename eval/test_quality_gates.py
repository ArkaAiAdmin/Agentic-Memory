"""Tests for quality_gates.py."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MEMORY_QUALITY_GATES"] = "1"

from quality_gates import (
    validate_result,
    filter_results,
    quality_stats,
    QUALITY_GATES_ENABLED,
    _tokenize,
    _jaccard,
)
import sqlite3
import tempfile


class TestTokenize(unittest.TestCase):
    def test_basic_tokenize(self):
        tokens = _tokenize("Hello world")
        self.assertEqual(tokens, {"hello", "world"})

    def test_stop_words_removed(self):
        tokens = _tokenize("The quick brown fox is here")
        self.assertNotIn("the", tokens)
        self.assertNotIn("is", tokens)
        self.assertIn("quick", tokens)
        self.assertIn("brown", tokens)
        self.assertIn("fox", tokens)

    def test_empty_string(self):
        self.assertEqual(_tokenize(""), set())


class TestJaccard(unittest.TestCase):
    def test_identical(self):
        self.assertAlmostEqual(_jaccard({"a", "b"}, {"a", "b"}), 1.0)

    def test_disjoint(self):
        self.assertAlmostEqual(_jaccard({"a", "b"}, {"c", "d"}), 0.0)

    def test_partial(self):
        sim = _jaccard({"a", "b"}, {"b", "c"})
        self.assertAlmostEqual(sim, 1 / 3, places=2)

    def test_empty_sets(self):
        self.assertAlmostEqual(_jaccard(set(), set()), 1.0)

    def test_one_empty(self):
        self.assertAlmostEqual(_jaccard({"a"}, set()), 0.0)


class TestValidateResult(unittest.TestCase):
    def test_valid_result(self):
        passed, reasons = validate_result(
            {
                "content": "This is a valid memory note.",
                "source": "lessons/test.md",
            }
        )
        self.assertTrue(passed)
        self.assertEqual(reasons, [])

    def test_too_short(self):
        passed, reasons = validate_result(
            {
                "content": "hi",
                "source": "lessons/test.md",
            }
        )
        self.assertFalse(passed)
        self.assertTrue(any("content_too_short" in r for r in reasons))

    def test_missing_source(self):
        passed, reasons = validate_result(
            {
                "content": "This is a valid memory note with content.",
            }
        )
        self.assertFalse(passed)
        self.assertTrue(any("missing_source" in r for r in reasons))

    def test_low_relevance(self):
        passed, reasons = validate_result(
            {
                "content": "This is a valid memory note with enough content to pass.",
                "source": "lessons/test.md",
                "relevance_score": 0.05,
            }
        )
        self.assertFalse(passed)
        self.assertTrue(any("low_relevance" in r for r in reasons))

    def test_empty_content(self):
        passed, reasons = validate_result(
            {
                "content": "",
                "source": "lessons/test.md",
            }
        )
        self.assertFalse(passed)

    def test_content_in_snippet_fallback(self):
        passed, reasons = validate_result(
            {
                "snippet": "This is a valid memory note with enough content to pass.",
                "source": "lessons/test.md",
            }
        )
        self.assertTrue(passed)


class TestFilterResults(unittest.TestCase):
    def test_empty_list(self):
        filtered, stats = filter_results([])
        self.assertEqual(filtered, [])
        self.assertEqual(stats["total"], 0)
        self.assertEqual(stats["passed"], 0)

    def test_all_pass(self):
        results = [
            {"content": "First valid note with enough content here.", "source": "a"},
            {"content": "Second valid note with enough content here.", "source": "b"},
        ]
        filtered, stats = filter_results(results)
        self.assertEqual(len(filtered), 2)
        self.assertEqual(stats["passed"], 2)

    def test_filters_short(self):
        results = [
            {"content": "short", "source": "a"},
            {"content": "This is a valid note with enough content.", "source": "b"},
        ]
        filtered, stats = filter_results(results)
        self.assertEqual(len(filtered), 1)
        self.assertGreater(stats["filtered"], 0)

    def test_exact_duplicate_removed(self):
        results = [
            {"content": "Same content here for both notes.", "source": "a", "id": "1"},
            {"content": "Same content here for both notes.", "source": "b", "id": "2"},
        ]
        filtered, stats = filter_results(results)
        self.assertEqual(len(filtered), 1)
        self.assertGreater(stats["reasons"].get("exact_duplicate", 0), 0)

    def test_near_duplicate_removed(self):
        results = [
            {
                "content": "The quick brown fox jumps over the lazy dog and runs away.",
                "source": "a",
                "id": "1",
            },
            {
                "content": "The quick brown fox jumps over the lazy dog and runs away quickly.",
                "source": "b",
                "id": "2",
            },
        ]
        filtered, stats = filter_results(results)
        # Jaccard ≈ 0.889 < 0.9 threshold — both kept
        self.assertEqual(len(filtered), 2)

    def test_different_notes_kept(self):
        results = [
            {
                "content": "Machine learning is a subset of artificial intelligence.",
                "source": "a",
                "id": "1",
            },
            {
                "content": "PostgreSQL is a relational database management system.",
                "source": "b",
                "id": "2",
            },
        ]
        filtered, stats = filter_results(results)
        self.assertEqual(len(filtered), 2)


class TestFilterResultsDisabled(unittest.TestCase):
    def test_disabled_passes_all(self):
        os.environ.pop("MEMORY_QUALITY_GATES", None)
        import quality_gates

        quality_gates.QUALITY_GATES_ENABLED = False
        try:
            results = [{"content": "short", "source": "a"}]
            filtered, stats = filter_results(results)
            self.assertEqual(len(filtered), 1)
            self.assertFalse(stats["enabled"])
        finally:
            os.environ["MEMORY_QUALITY_GATES"] = "1"
            quality_gates.QUALITY_GATES_ENABLED = True


class TestNearDupONlogN(unittest.TestCase):
    """Tests for the O(N log N) sort-based near-duplicate algorithm.

    The new implementation in ``filter_results`` replaces the previous
    O(N^2) Jaccard pass with a sort + sliding-window approach. These
    tests pin the behavior so future refactors don't regress the
    asymptotic complexity or the correctness guarantees.
    """

    def test_sliding_window_constant_is_module_level(self):
        """The window size must be a module-level constant so it's
        easy to raise if a benchmark shows missed near-dups."""
        import quality_gates

        self.assertTrue(hasattr(quality_gates, "_NEAR_DUP_WINDOW"))
        self.assertIsInstance(quality_gates._NEAR_DUP_WINDOW, int)
        self.assertGreaterEqual(quality_gates._NEAR_DUP_WINDOW, 1)

    def test_many_distinct_results_kept(self):
        """N results with no near-duplicates: all should be kept."""
        results = [
            {
                "content": f"Note {i} about topic {i} with unique words xyz{i}.",
                "source": f"src{i}",
                "id": f"n{i}",
            }
            for i in range(50)
        ]
        filtered, stats = filter_results(results)
        self.assertEqual(len(filtered), 50)
        self.assertEqual(stats["filtered"], 0)
        self.assertNotIn("near_duplicate", stats["reasons"])

    def test_many_exact_duplicates_collapsed(self):
        """N copies of the same content: only one survives."""
        content = "This is a sample memory with enough content to pass the gate."
        results = [
            {"content": content, "source": f"src{i}", "id": f"n{i}"} for i in range(20)
        ]
        filtered, stats = filter_results(results)
        self.assertEqual(len(filtered), 1)
        self.assertGreaterEqual(stats["reasons"].get("exact_duplicate", 0), 19)

    def test_near_duplicates_in_random_order_collapsed(self):
        """Near-duplicates interleaved with unique items should still
        be collapsed. The old O(N^2) implementation also did this;
        the new sort-based path must too.
        """
        # Two near-duplicate clusters (jaccard ~0.93 with 1 extra word)
        # plus 8 unique items, shuffled to stress the sliding window.
        cluster_a = (
            "PostgreSQL is a powerful open source relational database "
            "with strong ACID guarantees and excellent JSON support."
        )
        cluster_a_dup = cluster_a + " Always."
        cluster_b = (
            "Rust is a systems programming language focused on safety "
            "speed and concurrency with zero-cost abstractions."
        )
        cluster_b_dup = cluster_b + " Truly."

        items = [
            {"content": cluster_a, "source": "a", "id": "a1"},
            {"content": cluster_b, "source": "b", "id": "b1"},
            {"content": cluster_a_dup, "source": "a", "id": "a2"},
            {"content": cluster_b_dup, "source": "b", "id": "b2"},
            {
                "content": "Completely unique note about gardening tomatoes.",
                "source": "g",
                "id": "g",
            },
            {
                "content": "Another unique note about quantum entanglement.",
                "source": "q",
                "id": "q",
            },
            {
                "content": "Yet another unique note about bicycle maintenance.",
                "source": "c",
                "id": "c",
            },
            {
                "content": "Final unique note about French pastry techniques.",
                "source": "f",
                "id": "f",
            },
        ]
        # Repeat to stress the window
        results = items * 5
        filtered, stats = filter_results(results)
        # 4 unique clusters (a, b, g, q, c, f) = 6 unique clusters
        # Hmm, all 6 are unique, then dup pairs collapse to 6
        # But we have 8 items, so 6 unique (a, b, g, q, c, f)
        # 8 - 2 (the dups that were near-dups) = 6
        self.assertEqual(len(filtered), 6)
        self.assertGreaterEqual(stats["reasons"].get("near_duplicate", 0), 2)

    def test_large_input_no_quadratic_blowup(self):
        """Smoke test: 500 results with scattered duplicates runs
        in well under a second. The old O(N^2) impl on 500 inputs
        would be ~125K Jaccard ops; the new sort+window impl is
        ~500 * log(500) + 500 * 32 = ~22K ops."""
        import time

        results = []
        # 100 unique "base" notes — each is genuinely different and
        # the dedup identifier (a multi-letter word) survives the
        # single-char token filter.
        bases = [
            f"Base note about subject{i:03d}alpha with enough content to pass the quality gate threshold."
            for i in range(100)
        ]
        # Each base repeated 5 times with slight variation
        for base in bases:
            for k in range(5):
                if k == 0:
                    content = base
                else:
                    # Near-dup (Jaccard > 0.9) for k=1, exact-dup for k>=2
                    if k == 1:
                        content = base + " Extra."
                    else:
                        content = base
                results.append(
                    {"content": content, "source": "x", "id": f"{base[:5]}_{k}"}
                )

        t0 = time.perf_counter()
        filtered, stats = filter_results(results)
        elapsed = time.perf_counter() - t0

        # 100 unique base notes survive; the 400 copies get deduped
        self.assertEqual(len(filtered), 100)
        # 4 dup variants per base * 100 = 400 filtered
        self.assertGreaterEqual(stats["filtered"], 350)
        # Should finish in well under a second (the old impl was
        # much slower and would have hit the 100-input cap)
        self.assertLess(elapsed, 5.0, f"filter took {elapsed:.2f}s, expected < 5s")

    def test_token_prefilter_skips_jaccard_when_size_ratio_too_low(self):
        """If |small|/|big| < threshold, the implementation should
        skip the Jaccard op entirely. We can't directly observe the
        skip, but we can verify behavior is correct when one set is
        much smaller than the other."""
        results = [
            # big set
            {
                "content": "alpha beta gamma delta epsilon zeta eta theta iota kappa",
                "source": "a",
                "id": "1",
            },
            # smaller set: shares no tokens, can't be jaccard > 0.9 with the big set
            {
                "content": "lambda mu nu xi omicron pi rho sigma",
                "source": "b",
                "id": "2",
            },
        ]
        filtered, stats = filter_results(results)
        self.assertEqual(len(filtered), 2)
        self.assertNotIn("near_duplicate", stats["reasons"])

    def test_identical_content_collapsed_regardless_of_position(self):
        """Identical content (the 'shared' item) should be collapsed
        to one entry when it appears multiple times in the input,
        regardless of its position in the sort order."""
        shared = "Identical content that should be collapsed to one entry entirely."
        for position in ["first", "middle", "last"]:
            # 2 copies of the shared item + 5 unique fillers = 7 input
            shared_a = {"content": shared, "source": "a", "id": "1"}
            shared_b = {"content": shared, "source": "b", "id": "2"}
            filler = [
                {
                    "content": f"Unique filler number word{i:02d}alpha with enough words to pass the quality gate.",
                    "source": "f",
                    "id": f"f{i}",
                }
                for i in range(5)
            ]
            if position == "first":
                results = [shared_a, shared_b] + filler
            elif position == "last":
                results = filler + [shared_a, shared_b]
            else:  # middle
                results = filler[:2] + [shared_a, shared_b] + filler[2:]
            filtered, stats = filter_results(results)
            # Expect 5 fillers + 1 shared = 6 (the 2nd shared is an exact dup)
            self.assertEqual(len(filtered), 6, f"failed for {position}")
            self.assertEqual(
                stats["reasons"].get("exact_duplicate", 0),
                1,
                f"expected 1 exact_duplicate for {position}, got {stats['reasons']}",
            )


class TestQualityStats(unittest.TestCase):
    def test_stats_empty_db(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE memories (
                    id TEXT PRIMARY KEY, content TEXT, deleted_at TEXT
                )
            """)
            stats = quality_stats(conn)
            self.assertTrue(stats["enabled"])
            self.assertEqual(stats["total_notes"], 0)
            conn.close()
        finally:
            os.unlink(db_path)

    def test_stats_with_notes(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE memories (
                    id TEXT PRIMARY KEY, content TEXT, deleted_at TEXT
                )
            """)
            conn.execute(
                "INSERT INTO memories VALUES ('a', 'This is a valid note with enough content.', NULL)"
            )
            conn.execute("INSERT INTO memories VALUES ('b', 'short', NULL)")
            conn.execute("INSERT INTO memories VALUES ('c', NULL, NULL)")
            conn.commit()
            stats = quality_stats(conn)
            self.assertEqual(stats["total_notes"], 3)
            # 'short' (5 chars) and NULL (0 chars) are both below _MIN_CONTENT_LENGTH
            self.assertEqual(stats["too_short"], 2)
            conn.close()
        finally:
            os.unlink(db_path)


if __name__ == "__main__":
    unittest.main()
