"""p50/p95/p99 latency assertions for the search pipeline.

Runs 3 warm-up queries (WARM_COUNT=3) then 5 measured queries (MEASURE_COUNT=5)
for each of 3-5 representative query shapes (short keyword, medium phrase,
long sentence).  Logs individual times with logger.info and asserts:
  - p50 (median) < 500ms
  - p95 < 1500ms

Thresholds are generous — this is a regression gate against severe
performance regressions, not a performance target.  On constrained
hardware (CI, low-RAM), the thresholds give headroom.

Uses time.perf_counter() for wall-clock timing.

Decorated @pytest.mark.slow since it has real timing dependency.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import statistics
import sys
import tempfile
import time
import unittest
from pathlib import Path

import pytest

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from eval._fixtures import bootstrap_temp_db_clean

logger = logging.getLogger(__name__)

WARM_COUNT = 3
MEASURE_COUNT = 5

# Representative query shapes
SHORT_KEYWORD = "python async"
MEDIUM_PHRASE = "sqlite database indexing optimization"
LONG_SENTENCE = (
    "how do I set up machine learning pipeline with "
    "feature extraction model training and deployment"
)
CATEGORY_QUERY = "testing strategies"  # cross-category

QUERIES = [SHORT_KEYWORD, MEDIUM_PHRASE, LONG_SENTENCE, CATEGORY_QUERY]

# Thresholds (milliseconds) — generous for CI/hardware variance
P50_THRESHOLD_MS = 500
P95_THRESHOLD_MS = 1500

# Number of notes to seed
CORPUS_SIZE = 100

TOPICS = [
    "python programming language",
    "async programming asyncio",
    "sqlite database engine",
    "machine learning algorithms",
    "deep neural networks",
    "distributed systems",
    "microservices architecture",
    "rest api design patterns",
    "docker containerization",
    "kubernetes orchestration",
    "ci cd pipeline automation",
    "test driven development",
    "software architecture patterns",
    "data structures algorithms",
    "database indexing strategies",
    "query optimization techniques",
    "caching strategies redis",
    "event driven architecture",
    "domain driven design",
    "secure software development",
]


class TestSearchLatency(unittest.TestCase):
    """Latency regression gate for the search pipeline."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "memory.db"
        bootstrap_temp_db_clean(self.db_path)
        self._populate()
        from infra.db import connection_pool
        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()

    def tearDown(self):
        from infra.db import connection_pool
        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _populate(self, count: int = CORPUS_SIZE):
        """Insert *count* notes to provide a realistic corpus."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        now = "2026-07-14T12:00:00+00:00"
        categories = ["lessons", "decisions", "preferences"]
        for i in range(count):
            topic = TOPICS[i % len(TOPICS)]
            cat = categories[i % len(categories)]
            nid = f"{cat}/latency-note-{i:04d}"
            content = (
                f"Note {i}: Detailed content about {topic}. "
                f"This note contains information about {topic} for search index matching. "
                f"The system should find this when searching for related terms."
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

    def _run_timed_search(self, query: str, limit: int = 5) -> tuple[float, dict]:
        """Run a single search and return (elapsed_ms, result)."""
        from search.orchestrator import search_memories
        from infra.db import connection_pool

        # Clear pool to simulate cold start each time
        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()

        t0 = time.perf_counter()
        result = search_memories(self.db_path, query, limit=limit)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return elapsed_ms, result

    def _measure_query(self, query: str, label: str) -> list[float]:
        """Run WARM_COUNT warm-ups then MEASURE_COUNT measurements.
        Returns list of measured latencies in ms."""
        # Warm-up runs
        for _ in range(WARM_COUNT):
            self._run_timed_search(query)

        # Measured runs
        latencies = []
        for i in range(MEASURE_COUNT):
            elapsed_ms, result = self._run_timed_search(query)
            latencies.append(elapsed_ms)
            logger.info(
                "[%s] run %d: %.1f ms, %d results",
                label, i + 1, elapsed_ms, result.get("count", 0),
            )
            self.assertIsInstance(result, dict, f"Search failed for {query!r}")
            self.assertIn("results", result)

        return latencies

    @pytest.mark.slow
    def test_short_keyword_latency(self):
        latencies = self._measure_query(SHORT_KEYWORD, "short-keyword")
        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)]
        logger.info(
            "short-keyword: p50=%.1fms p95=%.1fms (thresholds %dms/%dms)",
            p50, p95, P50_THRESHOLD_MS, P95_THRESHOLD_MS,
        )
        self.assertLess(p50, P50_THRESHOLD_MS,
                        f"p50 {p50:.1f}ms >= {P50_THRESHOLD_MS}ms")
        self.assertLess(p95, P95_THRESHOLD_MS,
                        f"p95 {p95:.1f}ms >= {P95_THRESHOLD_MS}ms")

    @pytest.mark.slow
    def test_medium_phrase_latency(self):
        latencies = self._measure_query(MEDIUM_PHRASE, "medium-phrase")
        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)]
        logger.info(
            "medium-phrase: p50=%.1fms p95=%.1fms (thresholds %dms/%dms)",
            p50, p95, P50_THRESHOLD_MS, P95_THRESHOLD_MS,
        )
        self.assertLess(p50, P50_THRESHOLD_MS)
        self.assertLess(p95, P95_THRESHOLD_MS)

    @pytest.mark.slow
    def test_long_sentence_latency(self):
        latencies = self._measure_query(LONG_SENTENCE, "long-sentence")
        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)]
        logger.info(
            "long-sentence: p50=%.1fms p95=%.1fms (thresholds %dms/%dms)",
            p50, p95, P50_THRESHOLD_MS, P95_THRESHOLD_MS,
        )
        self.assertLess(p50, P50_THRESHOLD_MS)
        self.assertLess(p95, P95_THRESHOLD_MS)

    @pytest.mark.slow
    def test_cross_category_latency(self):
        latencies = self._measure_query(CATEGORY_QUERY, "cross-category")
        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)]
        logger.info(
            "cross-category: p50=%.1fms p95=%.1fms (thresholds %dms/%dms)",
            p50, p95, P50_THRESHOLD_MS, P95_THRESHOLD_MS,
        )
        self.assertLess(p50, P50_THRESHOLD_MS)
        self.assertLess(p95, P95_THRESHOLD_MS)

    @pytest.mark.slow
    def test_empty_query_latency(self):
        """Empty query should be fast (no FTS work)."""
        latencies = self._measure_query("", "empty")
        p50 = statistics.median(latencies)
        logger.info("empty-query: p50=%.1fms", p50)
        # Empty query should be very fast
        self.assertLess(p50, 200, f"Empty query p50 {p50:.1f}ms >= 200ms")


if __name__ == "__main__":
    unittest.main()
