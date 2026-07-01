#!/usr/bin/env python3
"""QW4: Cross-encoder rerank (token-overlap based).

Verifies that:
  1. _cross_encoder_score returns 0.0 for empty inputs
  2. _cross_encoder_score returns 0.0 for completely disjoint tokens
  3. _cross_encoder_score returns >0 for token matches
  4. _cross_encoder_score gives higher score when more query tokens match
  5. Phrase (bigram) bonus adds extra score beyond coverage
  6. Stopwords in query are down-weighted
  7. Duplicate query tokens don't dominate the score
  8. _apply_cross_encoder_rerank keeps tail results untouched
  9. _apply_cross_encoder_rerank can re-order head results
 10. CE-blend floor: a doc with ce=0 keeps at least 50% of its score
 11. Long content (bigger than query) still scores correctly
 12. _tokenize_for_ce lowercases and strips
 13. Integration: search_memories uses the CE (smoke test)
"""

import sys
import unittest
import tempfile
import shutil
import sqlite3
from pathlib import Path

# Make the memory_common importable.
THIS_DIR = Path(__file__).resolve().parent
AGENTIC = THIS_DIR.parent
sys.path.insert(0, str(AGENTIC))

import memory_mcp
from infra.memory_common import configure_logging
from _fixtures import bootstrap_temp_db_clean

configure_logging()


class TestCrossEncoderBasics(unittest.TestCase):
    def test_01_empty_query(self):
        self.assertEqual(memory_mcp._cross_encoder_score("", "hello world"), 0.0)

    def test_02_empty_content(self):
        self.assertEqual(memory_mcp._cross_encoder_score("hello", ""), 0.0)

    def test_03_disjoint_tokens(self):
        # "apple banana" vs "cat dog" — no shared tokens, no shared bigrams
        score = memory_mcp._cross_encoder_score("apple banana", "cat dog elephant")
        self.assertEqual(score, 0.0, f"disjoint should be 0, got {score}")

    def test_04_token_match_positive(self):
        score = memory_mcp._cross_encoder_score(
            "database migration", "we did a database migration today"
        )
        self.assertGreater(score, 0.0, "matching tokens should give positive score")

    def test_05_more_matches_higher(self):
        s1 = memory_mcp._cross_encoder_score(
            "database migration", "this is unrelated content about cats"
        )
        s2 = memory_mcp._cross_encoder_score(
            "database migration", "database migration guide for postgres"
        )
        self.assertGreater(
            s2, s1, f"more matches should give higher score: s1={s1} s2={s2}"
        )

    def test_06_phrase_bonus(self):
        # Same coverage, but the matching doc has the bigram too.
        s_no_phrase = memory_mcp._cross_encoder_score(
            "rate limit", "the limit on the rate was hit"
        )
        s_with_phrase = memory_mcp._cross_encoder_score(
            "rate limit", "we hit the rate limit yesterday"
        )
        self.assertGreater(
            s_with_phrase,
            s_no_phrase,
            f"phrase match should add bonus: with={s_with_phrase} without={s_no_phrase}",
        )

    def test_07_stopwords_downweighted(self):
        # The query "the" is a stopword, so its weight is tiny. The
        # content has "the" but not "database" — score should be small.
        s = memory_mcp._cross_encoder_score("the database", "the cat sat on the mat")
        # "database" is not in the content, so coverage is only on "the"
        # which is heavily down-weighted.
        self.assertLess(s, 0.2, f"missed-keyword + stopword-heavy should be small: {s}")

    def test_08_duplicate_query_tokens_dont_dominate(self):
        # Three "foo"s and one "bar"; doc has "foo" but no "bar".
        s_foo = memory_mcp._cross_encoder_score("foo foo foo bar", "foo foo foo bar")
        s_just_foo = memory_mcp._cross_encoder_score(
            "foo foo foo bar", "foo foo foo unrelated"
        )
        # The second query has bar missing, so the first should still win
        # (bar has weight ~0.66, foo*3 each weight ~0.66 so total is similar,
        # but the second loses a non-stopword word which has real weight).
        self.assertGreater(
            s_foo,
            s_just_foo,
            f"missing real words should drop score: full={s_foo} partial={s_just_foo}",
        )


class TestApplyCrossEncoderRerank(unittest.TestCase):
    def _make_result(self, note_id, content, score):
        """Build a scored-result 10-tuple."""
        return (
            note_id,
            content,
            f"{note_id}.md",
            "[]",
            "2024-01-01T00:00:00",
            -1.0,
            score,
            1.0,
            3,
            False,
        )

    def test_09_tail_untouched(self):
        # head = 2, tail = 2. The tail order must be preserved.
        head = [
            self._make_result("a", "alpha", 0.5),
            self._make_result("b", "beta", 0.6),
        ]
        tail = [
            self._make_result("c", "gamma", 0.4),
            self._make_result("d", "delta", 0.3),
        ]
        out = memory_mcp._apply_cross_encoder_rerank("alpha", head + tail, top_k=2)
        # Last two results must be c, d in that order (tail order preserved).
        self.assertEqual(out[-2][0], "c")
        self.assertEqual(out[-1][0], "d")
        # First two are head (in some order).
        self.assertEqual({out[0][0], out[1][0]}, {"a", "b"})

    def test_10_ce_can_reorder_head(self):
        # "a" has higher channel score but matches the query better.
        # "b" has lower channel score but content has nothing in common.
        # After CE, "a" should rank above "b" within head.
        head = [
            self._make_result("a", "alpha bravo charlie", 0.3),
            self._make_result("b", "completely unrelated content", 0.5),
        ]
        out = memory_mcp._apply_cross_encoder_rerank("alpha", head, top_k=2)
        # a should win because CE boosts it more
        self.assertEqual(
            out[0][0],
            "a",
            f"a (matches query) should rank above b (no match): {[(r[0], r[6]) for r in out]}",
        )
        # But b's score is also reduced (multiplied by ~0.5 baseline)
        # so it can't stay above a.
        self.assertGreater(out[0][6], out[1][6])

    def test_11_ce_floor_protects_unrelated_docs(self):
        # A doc that doesn't match the query at all (CE=0) should still
        # keep at least 50% of its channel score (the floor in the blend).
        head = [self._make_result("x", "absolutely nothing in common", 1.0)]
        out = memory_mcp._apply_cross_encoder_rerank("database", head, top_k=1)
        adjusted = out[0][6]
        self.assertGreaterEqual(
            adjusted,
            0.4,
            f"unrelated doc should keep ~50% of score via CE floor: {adjusted}",
        )

    def test_12_long_content(self):
        # Content much longer than query — coverage should still be high
        # if all query tokens are present.
        long_content = (
            "lorem ipsum dolor sit amet " * 50 + " database migration complete"
        )
        s = memory_mcp._cross_encoder_score("database migration", long_content)
        self.assertGreater(s, 0.7, f"long content covering all query terms: {s}")


class TestTokenizeForCe(unittest.TestCase):
    def test_13_lowercase_and_strip(self):
        toks = memory_mcp._tokenize_for_ce("Hello, World! Foo-Bar 123")
        # "foo-bar" is one token (the dash is part of the regex class)
        self.assertIn("hello", toks)
        self.assertIn("world", toks)
        self.assertIn("123", toks)
        # All should be lowercase
        for t in toks:
            self.assertEqual(t, t.lower())


class TestCrossEncoderIntegration(unittest.TestCase):
    """End-to-end: search_memories actually uses the CE (smoke test)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qw4_e2e_")
        self.db_path = Path(self.tmp) / "memory.db"
        # H21: use full prod schema (incl. FTS5 + triggers) instead of inline
        bootstrap_temp_db_clean(self.db_path)
        db = sqlite3.connect(str(self.db_path))
        now = "2024-06-01T00:00:00"
        # FTS triggers already created by bootstrap_temp_db_clean
        for i, (mid, txt) in enumerate(
            [
                ("m1", "discussion of database migration tooling"),
                ("m2", "the migration plan for production rollout"),
                ("m3", "unrelated notes about gardening and cooking"),
            ]
        ):
            db.execute(
                """INSERT INTO memories
                (id, content, source_file, tags, created_at, updated_at, observed_at,
                 fitness_score, importance, pinned)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (mid, txt, f"{mid}.md", "[]", now, now, now, 1.0, 3, 0),
            )
        db.commit()
        db.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_14_search_uses_ce(self):
        # search_memories should still return the matching docs.
        result = memory_mcp.search_memories(
            self.db_path,
            "database migration",
            limit=5,
            include_global=False,
            rerank=True,
            boost_pinned=True,
            recency_weight=0.1,
            include_invalid=True,
            hybrid=False,
        )
        ids = [r["id"] for r in result["results"]]
        # m1 has both "database" and "migration" — definitely matches.
        # m3 has neither — must NOT match.
        self.assertIn("m1", ids, f"m1 (matches query) should be in results: {ids}")
        self.assertNotIn("m3", ids, f"m3 (no match) should not be in results: {ids}")
        # Score for m1 should be positive (smoke test for CE path).
        m1_score = next(r["final_score"] for r in result["results"] if r["id"] == "m1")
        self.assertGreater(m1_score, 0.0, f"m1 should have positive score: {m1_score}")


if __name__ == "__main__":
    unittest.main()
