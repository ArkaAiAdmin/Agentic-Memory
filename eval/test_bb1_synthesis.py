#!/usr/bin/env python3
"""BB1: Multi-result synthesis.

Verifies that:
  1. _bb1_split_sentences handles empty text
  2. _bb1_split_sentences splits on period+space
  3. _bb1_split_sentences keeps URLs intact (no mid-URL splits)
  4. _bb1_split_sentences returns correct offsets
  5. _bb1_synthesize returns empty for empty input
  6. _bb1_synthesize picks the most relevant sentence per result
  7. _bb1_synthesize includes context sentences around the hit
  8. _bb1_synthesize skips results with no relevant content
  9. _bb1_synthesize respects max_sentences cap
 10. _bb1_synthesize ranks by ce * content_score
 11. _bb1_synthesize returns unique sources
 12. search_memories with synthesize=True returns a synthesis field
 13. search_memories with synthesize=False doesn't include synthesis
"""
import os
import sys
import unittest
import tempfile
import shutil
import sqlite3
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
AGENTIC = THIS_DIR.parent
sys.path.insert(0, str(AGENTIC))

import memory_mcp
from memory_common import configure_logging
from _fixtures import bootstrap_temp_db_clean
configure_logging()


class TestSentenceSplit(unittest.TestCase):
    def test_01_empty(self):
        self.assertEqual(memory_mcp._bb1_split_sentences(""), [])
        self.assertEqual(memory_mcp._bb1_split_sentences(None), [])

    def test_02_period_split(self):
        sents = memory_mcp._bb1_split_sentences("First sentence. Second sentence. Third.")
        self.assertEqual(len(sents), 3)
        # The text of each sentence
        self.assertIn("First sentence.", sents[0][2])
        self.assertIn("Second sentence.", sents[1][2])
        self.assertIn("Third.", sents[2][2])

    def test_03_preserves_urls(self):
        # Periods in URLs should not split the sentence.
        text = "Visit https://example.com/foo.bar for details. Then click here."
        sents = memory_mcp._bb1_split_sentences(text)
        # Should split into 2 sentences, not more.
        self.assertEqual(len(sents), 2, f"URLs should not split, got: {sents}")
        self.assertIn("https://example.com/foo.bar", sents[0][2])

    def test_04_offsets_correct(self):
        text = "First sentence. Second sentence."
        sents = memory_mcp._bb1_split_sentences(text)
        for s_off, e_off, txt in sents:
            self.assertEqual(text[s_off:e_off], txt)


class TestSynthesize(unittest.TestCase):
    def _make_result(self, note_id, content, score=1.0):
        """Build a scored-result 10-tuple for testing."""
        return (note_id, content, f"{note_id}.md", "[]",
                "2024-01-01T00:00:00", -1.0, score, 1.0, 3, False)

    def test_05_empty_input(self):
        self.assertEqual(memory_mcp._bb1_synthesize("query", []),
                         {"answer": "", "sentences": [], "sources": [], "skipped_low_relevance": 0})
        self.assertEqual(memory_mcp._bb1_synthesize("", [self._make_result("a", "content")]),
                         {"answer": "", "sentences": [], "sources": [], "skipped_low_relevance": 0})

    def test_06_picks_relevant_sentence(self):
        # Result has 2 sentences; only one matches the query.
        r = self._make_result("a", "Unrelated intro. The rate limit was set to 100/min. Conclusion.")
        synth = memory_mcp._bb1_synthesize("rate limit", [r])
        self.assertEqual(len(synth["sentences"]), 1)
        self.assertIn("rate limit", synth["sentences"][0]["sentence"].lower())
        # The context should include the surrounding sentences
        self.assertIn("100/min", synth["answer"])

    def test_07_context_includes_neighbors(self):
        # With context=1, the sentence before and after should be included
        r = self._make_result("a", "Before sentence. Target phrase. After sentence.")
        synth = memory_mcp._bb1_synthesize("target phrase", [r])
        self.assertIn("Before sentence", synth["answer"])
        self.assertIn("After sentence", synth["answer"])

    def test_08_skips_unrelated(self):
        # No sentence contains the query terms
        r = self._make_result("a", "All about cooking recipes here.")
        synth = memory_mcp._bb1_synthesize("database migration", [r])
        self.assertEqual(synth["answer"], "")
        self.assertEqual(synth["skipped_low_relevance"], 1)

    def test_09_max_sentences_cap(self):
        # 5 results, max_sentences=2
        results = [
            self._make_result(f"r{i}", f"Sentence {i}. Database migration notes here.")
            for i in range(5)
        ]
        synth = memory_mcp._bb1_synthesize("database migration", results, max_sentences=2)
        self.assertLessEqual(len(synth["sentences"]), 2)
        self.assertLessEqual(len(synth["sources"]), 2)

    def test_10_rank_by_combined_score(self):
        # Two results; one has higher content_score but same CE.
        # The higher-scored one should come first.
        r1 = self._make_result("a", "Database migration is critical. Always back up first.", score=0.5)
        r2 = self._make_result("b", "Database migration needs planning. Test before deploy.", score=2.0)
        synth = memory_mcp._bb1_synthesize("database migration", [r1, r2])
        # r2 should be first (higher content_score)
        self.assertEqual(synth["sentences"][0]["note_id"], "b",
            f"r2 (higher content_score) should rank first: {synth['sentences']}")

    def test_11_unique_sources(self):
        r1 = self._make_result("a", "Database migration is a process.")
        r2 = self._make_result("b", "Database migration requires planning.")
        synth = memory_mcp._bb1_synthesize("database migration", [r1, r2])
        self.assertEqual(set(synth["sources"]), {"a", "b"})


class TestSearchMemoriesSynthesis(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bb1_e2e_")
        self.db_path = Path(self.tmp) / "memory.db"
        # H21: use full prod schema (incl. FTS5 + triggers) instead of
        # inline minimal schema
        bootstrap_temp_db_clean(self.db_path)
        db = sqlite3.connect(str(self.db_path))
        now = "2024-06-01T00:00:00"
        for mid, txt in [
            ("lessons/db-1", "Database migration is critical. Always back up first. The team met on Monday."),
            ("lessons/db-2", "Unrelated content about cooking. Database migration needs planning. The chef was happy."),
            ("lessons/other", "All about cooking recipes here."),
        ]:
            db.execute("""INSERT INTO memories
                (id, content, source_file, tags, created_at, updated_at, observed_at,
                 fitness_score, importance, pinned)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (mid, txt, f"{mid}.md", "[]", now, now, now, 1.0, 3, 0))
        db.commit()
        db.close()
    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
    def test_12_synthesis_included(self):
        memory_mcp._search_cache.clear()
        result = memory_mcp.search_memories(
            self.db_path, "database migration", limit=5,
            include_global=False, rerank=True, boost_pinned=True,
            recency_weight=0.1, include_invalid=True, hybrid=False,
            synthesize=True,
        )
        self.assertIn("synthesis", result, "synthesize=True should add 'synthesis' field")
        self.assertGreater(len(result["synthesis"]["sentences"]), 0)
        # Should NOT include the unrelated "other" note
        self.assertNotIn("lessons/other", result["synthesis"]["sources"])
    def test_13_no_synthesis_by_default(self):
        memory_mcp._search_cache.clear()
        result = memory_mcp.search_memories(
            self.db_path, "database migration", limit=5,
            include_global=False, rerank=True, boost_pinned=True,
            recency_weight=0.1, include_invalid=True, hybrid=False,
        )
        self.assertNotIn("synthesis", result, "synthesize=False should NOT include synthesis")


if __name__ == "__main__":
    unittest.main()
