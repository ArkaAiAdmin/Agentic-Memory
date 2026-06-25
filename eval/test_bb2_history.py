#!/usr/bin/env python3
"""BB2: Conversation history resolution.

Verifies:
  1. _bb2_extract_terms drops stopwords and short tokens
  2. _bb2_extract_terms returns at most 8 terms
  3. _bb2_extract_terms dedups case-insensitively
  4. _bb2_is_reference_query detects pronouns
  5. _bb2_is_reference_query detects reference phrases
  6. _bb2_is_reference_query ignores normal queries
  7. _bb2_resolve with empty history is a no-op
  8. _bb2_resolve with no reference is a no-op
  9. _bb2_resolve appends terms from prior turn
 10. _bb2_resolve skips terms already in current query
 11. _bb2_record_turn adds an entry; ring buffer caps at 20
 12. _bb2_clear_history empties the buffer
 13. End-to-end: prior turn "database migration rates" enables "it" to find rate notes
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
from _fixtures import bootstrap_temp_db_clean


class TestExtractTerms(unittest.TestCase):
    def test_01_drops_stopwords(self):
        terms = memory_mcp._bb2_extract_terms("What is the database migration rate?")
        self.assertIn("database", terms)
        self.assertIn("migration", terms)
        self.assertIn("rate", terms)
        # Stopwords dropped
        for sw in ("the", "is", "a", "what"):
            self.assertNotIn(sw, terms)

    def test_02_max_eight(self):
        terms = memory_mcp._bb2_extract_terms("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu")
        self.assertLessEqual(len(terms), 8)

    def test_03_dedups(self):
        terms = memory_mcp._bb2_extract_terms("Database migration database migration database")
        # "database" and "migration" each appear once
        self.assertEqual(terms.count("database"), 1)
        self.assertEqual(terms.count("migration"), 1)


class TestIsReference(unittest.TestCase):
    def test_04_pronoun(self):
        self.assertTrue(memory_mcp._bb2_is_reference_query("it"))
        self.assertTrue(memory_mcp._bb2_is_reference_query("that"))
        self.assertTrue(memory_mcp._bb2_is_reference_query("What about it?"))

    def test_05_ref_phrase(self):
        self.assertTrue(memory_mcp._bb2_is_reference_query("more on that"))
        self.assertTrue(memory_mcp._bb2_is_reference_query("the previous one"))
        self.assertTrue(memory_mcp._bb2_is_reference_query("expand on this"))

    def test_06_normal_query(self):
        self.assertFalse(memory_mcp._bb2_is_reference_query("database migration rates"))
        self.assertFalse(memory_mcp._bb2_is_reference_query("how to configure postgresql"))
        self.assertFalse(memory_mcp._bb2_is_reference_query(""))


class TestResolve(unittest.TestCase):
    def setUp(self):
        memory_mcp._bb2_clear_history()
    def tearDown(self):
        memory_mcp._bb2_clear_history()
    def test_07_empty_history(self):
        res = memory_mcp._bb2_resolve("it")
        self.assertFalse(res["reused"])
        self.assertEqual(res["expanded_query"], "it")
    def test_08_no_reference(self):
        memory_mcp._bb2_record_turn("database migration rates", [])
        res = memory_mcp._bb2_resolve("how to configure postgres")
        self.assertFalse(res["reused"])
    def test_09_appends_terms(self):
        memory_mcp._bb2_record_turn("database migration rates", [])
        res = memory_mcp._bb2_resolve("it")
        self.assertTrue(res["reused"])
        self.assertIn("database", res["expanded_query"])
        self.assertIn("migration", res["expanded_query"])
    def test_10_skips_overlap(self):
        # Query already has "database" — only "migration" + "rates" should be added
        memory_mcp._bb2_record_turn("database migration rates", [])
        res = memory_mcp._bb2_resolve("database issue with it")
        self.assertIn("migration", res["added_terms"])
        self.assertNotIn("database", res["added_terms"])


class TestBuffer(unittest.TestCase):
    def setUp(self):
        memory_mcp._bb2_clear_history()
    def tearDown(self):
        memory_mcp._bb2_clear_history()
    def test_11_ring_buffer_cap(self):
        for i in range(25):
            memory_mcp._bb2_record_turn(f"query number {i}", [])
        self.assertEqual(len(memory_mcp._BB2_TURNS), 20)
        # First ones evicted — most recent is "query number 24"
        self.assertEqual(memory_mcp._BB2_TURNS[-1]["query"], "query number 24")
    def test_12_clear(self):
        memory_mcp._bb2_record_turn("test", [])
        memory_mcp._bb2_clear_history()
        self.assertEqual(len(memory_mcp._BB2_TURNS), 0)


class TestE2EHistoryResolution(unittest.TestCase):
    """End-to-end: record turn via search, then a follow-up reference query
    should find content the bare reference would not match."""
    def setUp(self):
        memory_mcp._bb2_clear_history()
        self.tmp = tempfile.mkdtemp(prefix="bb2_e2e_")
        self.db_path = Path(self.tmp) / "memory.db"
        # H21: use full prod schema (incl. FTS5 + triggers) instead of inline
        bootstrap_temp_db_clean(self.db_path)
        db = sqlite3.connect(str(self.db_path))
        now = "2024-06-01T00:00:00"
        for mid, txt in [
            ("notes/rate", "The retry rate was 100/min on first attempt, then backed off."),
            ("notes/migration", "The database migration step runs after authentication."),
            ("notes/unrelated", "All about cooking recipes here."),
        ]:
            db.execute("""INSERT INTO memories
                (id, content, source_file, tags, created_at, updated_at, observed_at,
                 fitness_score, importance, pinned)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (mid, txt, f"{mid}.md", "[]", now, now, now, 1.0, 3, 0))
        db.commit()
        db.close()
    def tearDown(self):
        memory_mcp._bb2_clear_history()
        shutil.rmtree(self.tmp, ignore_errors=True)
    def test_13_reference_finds_rate(self):
        memory_mcp._search_cache.clear()
        # First turn: literal query finds the rate note
        first = memory_mcp.search_memories(
            self.db_path, "retry rate backoff", limit=5,
            include_global=False, rerank=True, boost_pinned=True,
            recency_weight=0.1, include_invalid=True, hybrid=False,
        )
        # Manually record (the MCP wrapper does this; tests don't go through it)
        memory_mcp._bb2_record_turn("retry rate backoff", first.get("raw_results", []))
        self.assertGreater(first["count"], 0)
        # Second turn: bare "it" should still find the rate note
        # (the literal "it" query would match nothing; the resolved
        # query adds "retry rate backoff" back in)
        memory_mcp._search_cache.clear()
        resolved = memory_mcp._bb2_resolve("what about it")
        self.assertTrue(resolved["reused"], f"Should reuse: {resolved}")
        second = memory_mcp.search_memories(
            self.db_path, resolved["expanded_query"], limit=5,
            include_global=False, rerank=True, boost_pinned=True,
            recency_weight=0.1, include_invalid=True, hybrid=False,
        )
        ids = [r["id"] for r in second["results"]]
        self.assertIn("notes/rate", ids, f"Resolved query should find rate note: {ids}, expanded={resolved['expanded_query']}")


if __name__ == "__main__":
    unittest.main()
