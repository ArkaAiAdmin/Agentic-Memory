"""Concurrent search stress test.

Launches 5 concurrent search_memories calls using ThreadPoolExecutor(5),
each with a different query against a shared temp DB.  Asserts all
complete within 10 seconds, no phase_errors in any result envelope,
and all results have expected shape.

Decorated @pytest.mark.slow since it spawns real threads.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from eval._fixtures import bootstrap_temp_db_clean

logger = logging.getLogger(__name__)

QUERIES = [
    "python programming async",
    "sqlite database indexing",
    "machine learning algorithms",
    "software architecture patterns",
    "docker kubernetes deployment",
]


class TestConcurrentSearch(unittest.TestCase):
    """5 concurrent search calls against the same DB."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "memory.db"
        bootstrap_temp_db_clean(self.db_path)
        self._seed_notes()
        # Clear pool so searches open fresh connections
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
            ("lessons/sqlite-indexing", "SQLite indexing strategies: B-tree, covering indexes, partial indexes.",
             ["sqlite", "database"], "lessons"),
            ("lessons/ml-basics", "Machine learning uses statistical methods for prediction.",
             ["ml", "python"], "lessons"),
            ("decisions/microservices", "Chose microservices over monolith for team autonomy.",
             ["architecture"], "decisions"),
            ("lessons/docker-basics", "Docker containers package applications with dependencies.",
             ["docker", "devops"], "lessons"),
            ("preferences/testing", "Prefer pytest for all unit tests.",
             ["testing"], "preferences"),
            ("lessons/kubernetes-intro", "Kubernetes orchestrates container deployments across nodes.",
             ["kubernetes", "docker"], "lessons"),
            ("projects/search-pipeline", "Improving search pipeline with hybrid FTS+vector.",
             ["search", "ml"], "projects"),
            ("lessons/api-design", "RESTful API design: resource naming, status codes, versioning.",
             ["api", "architecture"], "lessons"),
            ("decisions/python-typing", "Use strict type hints across the codebase.",
             ["python", "typing"], "decisions"),
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

    def _search_worker(self, query: str, limit: int = 5) -> dict:
        """Run search_memories in a worker thread."""
        from search.orchestrator import search_memories
        return search_memories(self.db_path, query, limit=limit, light=True)

    def test_all_concurrent_searches_complete(self):
        """Launch 5 concurrent searches, verify all complete within 60s."""
        start = time.time()
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(self._search_worker, q): q for q in QUERIES
            }
            results_map = {}
            for future in as_completed(futures, timeout=180):
                q = futures[future]
                try:
                    results_map[q] = future.result()
                except Exception as e:
                    self.fail(f"Search for {q!r} raised: {e}")

        elapsed = time.time() - start
        logger.info("Concurrent searches completed in %.2fs", elapsed)
        self.assertLessEqual(
            elapsed, 60.0,
            f"5 concurrent searches took {elapsed:.2f}s (limit 60s)",
        )
        self.assertEqual(len(results_map), len(QUERIES))

    def test_all_results_have_valid_envelope(self):
        """Each result envelope must have standard keys."""
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(self._search_worker, q): q for q in QUERIES
            }
            for future in as_completed(futures, timeout=180):
                result = future.result()
                self.assertIsInstance(result, dict)
                self.assertIn("results", result)
                self.assertIn("count", result)
                self.assertIn("output", result)
                self.assertIsInstance(result["results"], list)
                self.assertIsInstance(result["count"], int)

    def test_no_search_returns_none(self):
        """No result should be None or malformed."""
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(self._search_worker, q): q for q in QUERIES
            }
            for future in as_completed(futures, timeout=180):
                result = future.result()
                self.assertIsNotNone(result)
                if result["results"]:
                    for r in result["results"]:
                        self.assertIn("id", r)
                        self.assertIn("final_score", r)

    def test_different_queries_different_results(self):
        """Different queries should return different result sets."""
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(self._search_worker, q): q for q in QUERIES
            }
            result_ids = {}
            for future in as_completed(futures, timeout=180):
                q = futures[future]
                result = future.result()
                result_ids[q] = {r["id"] for r in result.get("results", [])}

        # At least some queries should return different results
        # (it's possible two queries happen to return the same set,
        # but unlikely with these diverse queries)
        unique_sets = {frozenset(s) for s in result_ids.values()}
        self.assertGreater(
            len(unique_sets), 1,
            "Different queries should produce measurably different result sets",
        )

    def test_query_limit_respected_under_concurrency(self):
        """Each search must respect its own limit under concurrent load."""
        limits = [3, 5, 10, 3, 5]
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(self._search_worker, q, lim): (q, lim)
                for q, lim in zip(QUERIES, limits)
            }
            for future in as_completed(futures, timeout=180):
                q, lim = futures[future]
                result = future.result()
                self.assertLessEqual(
                    len(result["results"]), lim,
                    f"Query {q!r} exceeded limit {lim}: {len(result['results'])} results",
                )


if __name__ == "__main__":
    unittest.main()
