"""Large-corpus regression test for the search pipeline.

Populates 200+ memories in a temp DB (with varied content across categories)
and exercises the full search orchestrator to ensure the pipeline completes
without error.  This is NOT a performance benchmark — no timing thresholds.

Maintains the invariant: result count <= limit, envelope is structurally
valid, and the pipeline never raises an unhandled exception.

See also: eval/profile_search.py (performance profiling).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from eval._fixtures import bootstrap_temp_db_clean

logger = logging.getLogger(__name__)

# Number of fake notes to insert
CORPUS_SIZE = 250
TOPICS = [
    "python programming language",
    "async programming asyncio",
    "sqlite database engine",
    "machine learning algorithms",
    "deep neural networks",
    "natural language processing",
    "computer vision applications",
    "distributed systems design",
    "microservices architecture",
    "rest api design patterns",
    "docker containerization",
    "kubernetes orchestration",
    "ci cd pipeline automation",
    "test driven development",
    "continuous integration",
    "git version control",
    "agile software development",
    "code review best practices",
    "software architecture patterns",
    "object oriented design",
    "functional programming",
    "data structures algorithms",
    "time complexity analysis",
    "database indexing strategies",
    "query optimization techniques",
    "caching strategies redis",
    "message queue systems",
    "event driven architecture",
    "domain driven design",
    "api gateway patterns",
    "oauth authentication",
    "jwt token management",
    "secure software development",
    "threat modeling",
    "penetration testing",
    "monitoring observability",
    "prometheus metrics",
    "grafana dashboards",
    "log aggregation elk",
    "distributed tracing",
    "service mesh istio",
    "cloud native patterns",
    "aws lambda serverless",
    "terraform infrastructure",
    "immutable infrastructure",
    "blue green deployment",
    "canary releases",
    "feature flags toggle",
    "a b testing",
    "performance benchmarking",
]


class TestLargeCorpusRegression(unittest.TestCase):
    """Regression test with 200+ seeded notes."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "memory.db"
        bootstrap_temp_db_clean(self.db_path)
        self._populate()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _populate(self, count: int = CORPUS_SIZE):
        """Insert *count* notes with varied content and categories."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        now = "2026-07-14T12:00:00+00:00"

        categories = ["lessons", "decisions", "preferences", "projects"]
        for i in range(count):
            topic = TOPICS[i % len(TOPICS)]
            cat = categories[i % len(categories)]
            nid = f"{cat}/note-{i:04d}"
            content = (
                f"Note {i}: This is a detailed note about {topic}. "
                f"It contains multiple sentences for the search index to match. "
                f"The topic of {topic} is important for understanding the system. "
            )
            source = f"memory/{cat}/{nid}.md"
            tags = json.dumps([topic.split()[0], cat])
            conn.execute(
                "INSERT INTO memories "
                "(id, content, source_file, tags, created_at, updated_at, observed_at, category) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (nid, content, source, tags, now, now, now, cat),
            )
        conn.commit()
        conn.close()

    def _import_search(self):
        from search.orchestrator import search_memories
        return search_memories

    def _clear_connection_pool(self):
        from infra.db import connection_pool
        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()

    # -- query shapes -----------------------------------------------------

    def test_short_keyword_query(self):
        self._clear_connection_pool()
        search_memories = self._import_search()
        result = search_memories(self.db_path, "python programming", limit=10)
        self.assertIsInstance(result, dict)
        self.assertIn("results", result)
        self.assertLessEqual(len(result["results"]), 10)
        self.assertIn("count", result)
        self.assertIn("output", result)

    def test_medium_phrase_query(self):
        self._clear_connection_pool()
        search_memories = self._import_search()
        result = search_memories(
            self.db_path, "database indexing and query optimization", limit=5
        )
        self.assertIsInstance(result, dict)
        self.assertLessEqual(len(result["results"]), 5)

    def test_long_sentence_query(self):
        self._clear_connection_pool()
        search_memories = self._import_search()
        result = search_memories(
            self.db_path,
            "How do I set up a CI CD pipeline with automated testing "
            "and deploy to kubernetes using docker containers",
            limit=8,
        )
        self.assertIsInstance(result, dict)
        self.assertLessEqual(len(result["results"]), 8)

    def test_cross_category_search(self):
        self._clear_connection_pool()
        search_memories = self._import_search()
        result = search_memories(self.db_path, "testing strategies", limit=10)
        self.assertIsInstance(result, dict)
        self.assertGreaterEqual(result["count"], 0)

    def test_result_envelope_structure(self):
        self._clear_connection_pool()
        search_memories = self._import_search()
        result = search_memories(self.db_path, "python", limit=5)
        self.assertIsInstance(result, dict)
        for r in result.get("results", []):
            self.assertIn("id", r)
            self.assertIn("final_score", r)
            self.assertIn("content", r)
            self.assertIsInstance(r["final_score"], (int, float))
            self.assertIsInstance(r["id"], str)

    def test_limit_boundary_low(self):
        self._clear_connection_pool()
        search_memories = self._import_search()
        result = search_memories(self.db_path, "programming", limit=1)
        self.assertLessEqual(len(result["results"]), 1)

    def test_limit_boundary_high(self):
        self._clear_connection_pool()
        search_memories = self._import_search()
        result = search_memories(self.db_path, "programming", limit=100)
        # The orchestrator may cap at some value, but should not exceed limit
        self.assertLessEqual(len(result["results"]), 100)

    def test_empty_query_no_crash(self):
        self._clear_connection_pool()
        search_memories = self._import_search()
        result = search_memories(self.db_path, "", limit=5)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["count"], 0)

    def test_special_characters_query(self):
        self._clear_connection_pool()
        search_memories = self._import_search()
        result = search_memories(self.db_path, "!@#$%^&*()", limit=5)
        self.assertIsInstance(result, dict)
        # Should not crash — may return 0 results
        self.assertIn("count", result)

    def test_corpus_size_correct(self):
        """Verify we actually inserted all notes."""
        conn = sqlite3.connect(str(self.db_path))
        count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        conn.close()
        self.assertEqual(count, CORPUS_SIZE)


if __name__ == "__main__":
    unittest.main()
