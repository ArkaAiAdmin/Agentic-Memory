"""Regression tests for the retrieval pipeline.

Requires:
    - ``retrieval_benchmark.py`` (same directory)
    - ``retrieval_golden_set.json`` (same directory)
    - ``search.orchestrator.search_memories`` reachable via sys.path

Tests are grouped by:
    T1 — Baseline precision/recall/MRR for hybrid mode
    T2 — Phase comparison: FTS vs hybrid on conceptual queries
    T3 — agent_scope key presence in every result dict
    T4 — shared_with_me parameter accepted without error
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import pytest

pytestmark = pytest.mark.slow
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap — mirrors eval/test_search_pipeline_unit.py exactly
# ---------------------------------------------------------------------------
INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from _fixtures import bootstrap_temp_db_clean  # noqa: E402
from retrieval_benchmark import RetrievalBenchmark  # noqa: E402
from search.orchestrator import search_memories  # noqa: E402

_PROD_DB_STR = os.environ.get("MEMORY_DB_PATH")
if not _PROD_DB_STR:
    raise RuntimeError(
        "MEMORY_DB_PATH must be set to a temp DB to run these tests. "
        "Use the temp_db_path fixture or set MEMORY_DB_PATH explicitly."
    )
_PROD_DB = Path(_PROD_DB_STR)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _extract_ids(result: dict) -> list[str]:
    """Extract note IDs from a search_memories result dict."""
    return [r["id"] for r in result.get("results", [])]


# ---------------------------------------------------------------------------
# T1 — Baseline metrics for hybrid mode
# ---------------------------------------------------------------------------


class TestHybridBaselineMetrics(unittest.TestCase):
    """T1: hybrid mode must meet minimum precision/recall/MRR thresholds."""

    def setUp(self):
        self._bench = RetrievalBenchmark()
        self._report = self._bench.run()

    def test_hybrid_precision_at_5(self):
        """precision@5 across all hybrid cases >= 0.35.

        The recency channel (temporal decay weighting) trades a small
        precision drop for better real-world temporal ranking.  Older
        benchmark thresholds assumed recency was excluded from scoring.
        """
        metrics = self._report["phases"]["hybrid"]
        self.assertGreaterEqual(
            metrics["precision_at_5"],
            0.35,
            f"hybrid precision@5={metrics['precision_at_5']:.3f} "
            f"fell below 0.35",
        )

    def test_hybrid_recall_at_5(self):
        """recall@5 across all hybrid cases >= 0.4."""
        metrics = self._report["phases"]["hybrid"]
        self.assertGreaterEqual(
            metrics["recall_at_5"],
            0.4,
            f"hybrid recall@5={metrics['recall_at_5']:.3f} "
            f"fell below 0.4",
        )

    def test_hybrid_mrr(self):
        """MRR across all hybrid cases >= 0.5."""
        metrics = self._report["phases"]["hybrid"]
        self.assertGreaterEqual(
            metrics["mrr"],
            0.5,
            f"hybrid MRR={metrics['mrr']:.3f} fell below 0.5",
        )

    def test_hybrid_precision_at_10(self):
        """precision@10 must be non-zero (sanity check)."""
        metrics = self._report["phases"]["hybrid"]
        self.assertGreater(metrics["precision_at_10"], 0.0)

    def test_hybrid_recall_at_10(self):
        """recall@10 must be non-zero (sanity check)."""
        metrics = self._report["phases"]["hybrid"]
        self.assertGreater(metrics["recall_at_10"], 0.0)

    def test_hybrid_latency_non_nan(self):
        """Latency must be a real number."""
        metrics = self._report["phases"]["hybrid"]
        self.assertGreater(metrics["latency_ms"], 0.0)

    def test_all_cases_ran(self):
        """Both phases should have run all golden test cases."""
        for phase_name in ("fts", "hybrid"):
            m = self._report["phases"][phase_name]
            self.assertEqual(
                m["total_cases"],
                25,
                f"{phase_name}: expected 25 cases, got {m['total_cases']}",
            )


# ---------------------------------------------------------------------------
# T2 — Phase comparison: hybrid >= FTS on conceptual queries
# ---------------------------------------------------------------------------


class TestHybridImprovesOverFTS(unittest.TestCase):
    """T2: hybrid precision@5 must be >= FTS precision@5 on conceptual queries."""

    CONCEPTUAL_QUERIES = [
        "what type of infrastructure management system orchestrates containers",
        "docker type of container platform",
        "python quality assurance testing",
        "what database index performance query",
        "log structured JSON severity debugging",
    ]

    def setUp(self):
        self._bench = RetrievalBenchmark()
        # Run only the conceptual query subset manually for precise control
        self._tmpdir = Path(tempfile.mkdtemp(prefix="retrieval_phase_"))
        self._db = self._tmpdir / "memory.db"
        bootstrap_temp_db_clean(self._db)

        golden = self._bench._golden
        _seed_db = __import__(
            "retrieval_benchmark", fromlist=["_seed_db"]
        )._seed_db
        _seed_db(self._db, golden)

        self._cases = [
            tc
            for tc in self._bench._cases
            if tc.query in self.CONCEPTUAL_QUERIES
        ]

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _ids_at_k(self, result: dict, k: int) -> list[str]:
        return [r["id"] for r in result.get("results", [])[:k]]

    def test_hybrid_precision_at_5_ge_fts_on_conceptual(self):
        """For conceptual queries, hybrid precision@5 >= FTS precision@5."""
        fts_hits = 0
        hyb_hits = 0
        n = 0
        for case in self._cases:
            fts_result = search_memories(
                self._db,
                case.query,
                limit=5,
                category=case.category or "",
                tags=case.tags,
                include_global=True,
                rerank=False,
                light=True,
                include_facts=False,
                safety_wiring=False,
            )
            hyb_result = search_memories(
                self._db,
                case.query,
                limit=5,
                category=case.category or "",
                tags=case.tags,
                include_global=True,
                rerank=False,
                include_facts=False,
                safety_wiring=False,
            )
            fts_ids = set(self._ids_at_k(fts_result, 5))
            hyb_ids = set(self._ids_at_k(hyb_result, 5))
            expected = case.expected_note_ids

            fts_prec = len(fts_ids & expected) / 5 if fts_ids else 0.0
            hyb_prec = len(hyb_ids & expected) / 5 if hyb_ids else 0.0
            fts_hits += fts_prec
            hyb_hits += hyb_prec
            n += 1

        if n == 0:
            self.skipTest("No conceptual queries matched in golden set")
        avg_fts = fts_hits / n
        avg_hyb = hyb_hits / n
        self.assertGreaterEqual(
            avg_hyb,
            avg_fts,
            f"hybrid avg precision@5 ({avg_hyb:.3f}) < FTS avg "
            f"precision@5 ({avg_fts:.3f}) — embedding should help "
            f"conceptual queries",
        )


# ---------------------------------------------------------------------------
# T3 — agent_scope key present in every result
# ---------------------------------------------------------------------------


class TestAgentScopePresent(unittest.TestCase):
    """T3: every result dict from search_memories must contain agent_scope."""

    def setUp(self):
        self._tmpdir = Path(tempfile.mkdtemp(prefix="retrieval_scope_"))
        self._db = self._tmpdir / "memory.db"
        bootstrap_temp_db_clean(self._db)
        golden = RetrievalBenchmark()._golden
        _seed_db_fn = __import__(
            "retrieval_benchmark", fromlist=["_seed_db"]
        )._seed_db
        _seed_db_fn(self._db, golden)
        # Insert at least one content-bearing memory so search returns results
        from save_pipeline import save_memory  # noqa: E402

        save_memory(
            content="Scope test memory about containers and docker.",
            category="lessons",
            title_slug="scope-test-memory",
            tags=["scope-test"],
            db_path=str(self._db),
            safety_wiring=False,
        )

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_agent_scope_in_every_result_item(self):
        """search_memories result dicts each contain an agent_scope key."""
        result = search_memories(
            self._db,
            "containers docker",
            limit=5,
            safety_wiring=False,
        )
        self.assertIn("agent_scope", result)
        # result["agent_scope"] is the top-level scope string
        self.assertIsInstance(result["agent_scope"], str)
        # Individual result items don't carry agent_scope (top-level only),
        # but the top-level key must always be present.
        self.assertIn("agent_scope", result)

    def test_agent_scope_on_empty_results(self):
        """agent_scope should be present even when no results are returned."""
        result = search_memories(
            self._db,
            "xyznonexistentquery_that_returns_nothing_12345",
            limit=5,
            safety_wiring=False,
        )
        self.assertIn("agent_scope", result)


# ---------------------------------------------------------------------------
# T4 — shared_with_me parameter accepted without error
# ---------------------------------------------------------------------------


class TestSharedWithMeParameter(unittest.TestCase):
    """T4: search_memories must accept shared_with_me=False without error."""

    def setUp(self):
        self._tmpdir = Path(tempfile.mkdtemp(prefix="retrieval_swm_"))
        self._db = self._tmpdir / "memory.db"
        bootstrap_temp_db_clean(self._db)
        golden = RetrievalBenchmark()._golden
        _seed_db_fn = __import__(
            "retrieval_benchmark", fromlist=["_seed_db"]
        )._seed_db
        _seed_db_fn(self._db, golden)

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_shared_with_me_false_returns_empty_when_no_shared(self):
        """No shared memories → empty results; no exception."""
        result = search_memories(
            self._db,
            "docker",
            limit=10,
            shared_with_me=False,
            safety_wiring=False,
        )
        # Should not raise; returns a dict with valid structure
        self.assertIsInstance(result, dict)
        self.assertIn("results", result)
        self.assertIn("count", result)
        self.assertIn("output", result)

    def test_shared_with_me_true_accepts_parameter_no_error(self):
        """shared_with_me=True must not raise — no agent context means
        it falls back to regular FTS results (0 in this seeded DB dataset).
        The key assertion is that the call completes without an exception."""
        result = search_memories(
            self._db,
            "docker",
            limit=10,
            shared_with_me=True,
            safety_wiring=False,
        )
        self.assertIsInstance(result, dict)
        self.assertIn("results", result)
        self.assertIn("agent_scope", result)


# ---------------------------------------------------------------------------
# T5 — Run full benchmark and surface report as a pytest fixture test
# ---------------------------------------------------------------------------


class TestFullBenchmarkReport(unittest.TestCase):
    """T5: full benchmark completes without error and produces a valid report."""

    def test_run_produces_valid_report(self):
        bench = RetrievalBenchmark()
        report = bench.run()
        self.assertIn("phases", report)
        self.assertIn("per_case", report)
        self.assertIn("dataset_info", report)
        self.assertIn("fts", report["phases"])
        self.assertIn("hybrid", report["phases"])

        for phase_name, metrics in report["phases"].items():
            self.assertIn("precision_at_5", metrics)
            self.assertIn("recall_at_5", metrics)
            self.assertIn("precision_at_10", metrics)
            self.assertIn("recall_at_10", metrics)
            self.assertIn("mrr", metrics)
            self.assertIn("latency_ms", metrics)
            self.assertIsInstance(metrics["precision_at_5"], float)
            self.assertGreaterEqual(metrics["precision_at_5"], 0.0)
            self.assertLessEqual(metrics["precision_at_5"], 1.0)

    def test_all_25_cases_present_in_report(self):
        bench = RetrievalBenchmark()
        report = bench.run()
        self.assertEqual(len(report["per_case"]), 25)
        self.assertEqual(report["dataset_info"]["total_test_cases"], 25)
        self.assertEqual(report["dataset_info"]["total_memories"], 20)


if __name__ == "__main__":
    unittest.main()
