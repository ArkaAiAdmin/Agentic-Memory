"""Rank-lock invariant test via the full search_memories call chain.

Read eval/test_search_rank_lock.py first — that file tests the
enrichment function directly.  This file tests the same invariant
through the full orchestrator entry point:

  * After CE reranking owns the final ORDER, no later enrichment step
    may change the relative order of results.
  * Enrichment envelope fields (concept_boost, centrality_boost,
    jaccard_surprise, temporal_decay) never appear in the final_score.

Runs 3-5 queries through search_memories, asserts results are sorted
by descending final_score, and verifies the no-mutation contract.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from eval._fixtures import bootstrap_temp_db_clean

QUERIES = [
    "python programming",
    "sqlite database",
    "machine learning",
    "software architecture",
    "testing strategies",
]


class TestSearchRankLockProduction(unittest.TestCase):
    """Rank-lock invariant through the full orchestrator call chain."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "memory.db"
        bootstrap_temp_db_clean(self.db_path)
        self._seed_notes()
        from infra.db import connection_pool
        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()

    def tearDown(self):
        from infra.db import connection_pool
        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed_notes(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        now = "2026-07-14T12:00:00+00:00"
        notes = [
            ("lessons/python-async", "Async Python with asyncio enables concurrent I/O.",
             ["python", "async"], "lessons"),
            ("lessons/python-basics", "Python is a popular programming language for data science.",
             ["python"], "lessons"),
            ("lessons/sqlite-wal", "SQLite WAL mode allows concurrent reads during writes.",
             ["sqlite"], "lessons"),
            ("lessons/sqlite-indexing", "SQLite indexing strategies for query performance.",
             ["sqlite", "database"], "lessons"),
            ("lessons/ml-intro", "Machine learning uses statistical methods for prediction.",
             ["ml", "python"], "lessons"),
            ("lessons/ml-models", "Common ML models: linear regression, random forest, neural nets.",
             ["ml"], "lessons"),
            ("decisions/microservices", "Chose microservices over monolith for team autonomy.",
             ["architecture"], "decisions"),
            ("decisions/python-typing", "Use strict type hints across the codebase.",
             ["python", "typing"], "decisions"),
            ("preferences/testing", "Prefer pytest for all new test files.",
             ["testing"], "preferences"),
            ("preferences/editor", "Use VSCode with Python extension for development.",
             ["editor", "python"], "preferences"),
            ("lessons/docker-basics", "Docker containers package applications with dependencies.",
             ["docker", "devops"], "lessons"),
            ("lessons/api-design", "RESTful API design: resource naming, status codes, versioning.",
             ["api", "architecture"], "lessons"),
            ("projects/search-pipeline", "Improving search pipeline with hybrid FTS+vector search.",
             ["search", "ml"], "projects"),
            ("projects/kg-enhance", "Enhancing knowledge graph with temporal facts.",
             ["knowledge-graph"], "projects"),
        ]
        for nid, content, tags, category in notes:
            source = f"memory/{category}/{nid}.md"
            conn.execute(
                "INSERT INTO memories (id,content,source_file,tags,created_at,updated_at,observed_at,category) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (nid, content, source, json.dumps(tags), now, now, now, category),
            )
        conn.commit()
        conn.close()

    def _search(self, query: str, limit: int = 10) -> dict:
        from search.orchestrator import search_memories
        from infra.db import connection_pool
        try:
            return search_memories(self.db_path, query, limit=limit)
        finally:
            connection_pool._pool.clear()
            connection_pool._pooled_ids.clear()

    # -- invariant: results sorted by final_score descending ---------------

    def test_results_sorted_by_final_score_descending(self):
        for q in QUERIES:
            with self.subTest(query=q):
                result = self._search(q, limit=10)
                results = result.get("results", [])
                if len(results) < 2:
                    continue  # can't test ordering with <2 results
                scores = [r["final_score"] for r in results]
                for i in range(len(scores) - 1):
                    self.assertGreaterEqual(
                        scores[i], scores[i + 1],
                        f"Query {q!r}: results not sorted descending at "
                        f"index {i}: {scores[i]} < {scores[i + 1]}",
                    )

    # -- invariant: enrichment fields are NOT in the score column ----------

    def test_enrichment_fields_not_in_final_score(self):
        """Verify that concept_boost etc. are distinct from final_score."""
        for q in QUERIES:
            with self.subTest(query=q):
                result = self._search(q, limit=10)
                for r in result.get("results", []):
                    fs = r.get("final_score", 0)
                    # The enrichment fields should be separate envelope keys
                    cb = r.get("concept_boost", 1.0)
                    jb = r.get("jaccard_surprise", 1.0)
                    td = r.get("temporal_decay", 1.0)
                    # Concept boost should NOT equal final_score
                    # (if it does, the enrichment leaked into ordering)
                    self.assertNotEqual(
                        fs, cb,
                        f"final_score equals concept_boost (enrichment leaked into order) "
                        f"for {r.get('id')}",
                    )
                    # Jaccard surprise should be a float
                    self.assertIsInstance(jb, float)
                    self.assertIsInstance(td, float)

    # -- invariant: envelope fields always present -------------------------

    def test_envelope_fields_always_present(self):
        for q in QUERIES:
            with self.subTest(query=q):
                result = self._search(q, limit=10)
                for r in result.get("results", []):
                    self.assertIn("concept_boost", r,
                                  f"Missing concept_boost in {r.get('id')}")
                    self.assertIn("centrality_boost", r,
                                  f"Missing centrality_boost in {r.get('id')}")
                    self.assertIn("jaccard_surprise", r,
                                  f"Missing jaccard_surprise in {r.get('id')}")
                    self.assertIn("temporal_decay", r,
                                  f"Missing temporal_decay in {r.get('id')}")

    # -- edge: result envelope structure ----------------------------------

    def test_result_envelope_has_expected_keys(self):
        for q in QUERIES:
            with self.subTest(query=q):
                result = self._search(q, limit=5)
                self.assertIn("results", result)
                self.assertIn("count", result)
                self.assertIn("output", result)
                for r in result.get("results", []):
                    self.assertIn("id", r)
                    self.assertIn("content", r)
                    self.assertIn("final_score", r)
                    self.assertIn("source_file", r)
                    self.assertIn("category", r)

    # -- multiple queries sanity ------------------------------------------

    def test_different_queries_produce_different_results(self):
        """Sanity: different queries should produce different top results."""
        top_ids = {}
        for q in QUERIES:
            result = self._search(q, limit=3)
            top_ids[q] = [r["id"] for r in result.get("results", [])]

        # At least some queries should differ (it would be very suspicious
        # if all 5 queries returned identical results)
        unique = {tuple(ids) for ids in top_ids.values()}
        self.assertGreater(
            len(unique), 1,
            "All queries returned identical top results — something is wrong",
        )

    # -- edge: empty query -------------------------------------------------

    def test_empty_query_returns_empty_results(self):
        result = self._search("", limit=5)
        self.assertEqual(result["count"], 0)
        self.assertEqual(len(result["results"]), 0)

    # -- edge: result_count_matches ---------------------------------------

    def test_count_matches_results_length(self):
        for q in QUERIES[:2]:
            with self.subTest(query=q):
                result = self._search(q, limit=5)
                self.assertEqual(
                    result["count"], len(result["results"]),
                    f"count {result['count']} != len(results) {len(result['results'])} "
                    f"for query {q!r}",
                )


if __name__ == "__main__":
    unittest.main()
