"""Unit tests for the unified benchmarking framework."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from eval.bench.protocol import BenchmarkQuestion, BenchmarkSession
from eval.bench.metrics import (
    calculate_latency_stats,
    compute_lafs,
    compute_retrieval_metrics,
    compute_text_metrics,
    compute_token_f1,
)
from eval.bench.db_manager import BenchmarkDBManager
from eval.bench.engine import BenchmarkHarness
from eval.bench.adapters import AdversarialAdapter, GoldenAdapter, LoCoMoAdapter, BaseBenchmarkAdapter
from eval.run_benchmarks import check_regression


def test_compute_retrieval_metrics():
    retrieved = ["doc_1", "doc_2", "doc_3", "doc_4", "doc_5"]
    gold = {"doc_2", "doc_4"}

    metrics = compute_retrieval_metrics(retrieved, gold, ks=(1, 2, 5))
    assert metrics["mrr"] == 0.5  # First hit is at rank 2 -> 1/2
    assert metrics["recall@1"] == 0.0
    assert metrics["recall@2"] == 0.5  # 1/2
    assert metrics["recall@5"] == 1.0  # 2/2
    assert metrics["precision@2"] == 0.5  # 1/2
    assert metrics["precision@5"] == 0.4  # 2/5
    assert metrics["ndcg@5"] > 0.0

    # Test empty gold returns 0.0, not inflated 1.0
    empty_gold_metrics = compute_retrieval_metrics(retrieved, set(), ks=(1, 5))
    assert empty_gold_metrics["recall@1"] == 0.0
    assert empty_gold_metrics["mrr"] == 0.0


def test_compute_text_metrics():
    # Exact match
    em_res = compute_text_metrics("Paris, France", "Paris, France")
    assert em_res["exact_match"] == 1.0
    assert em_res["overall_accuracy"] == 1.0

    # Substring match
    sub_res = compute_text_metrics("The capital is Paris, France today.", "Paris, France")
    assert sub_res["substring_match"] == 1.0
    assert sub_res["overall_accuracy"] == 1.0

    # Token F1 multiset check
    f1 = compute_token_f1("apple apple banana cherry", "apple banana cherry")
    assert 0.8 < f1 < 0.9

    # Rubric compliance
    rubric_res = compute_text_metrics(
        "We used python 3.11 with sqlite3 and faiss backend.",
        "",
        rubric=["python 3.11", "sqlite3"],
    )
    assert rubric_res["rubric_score"] == 1.0
    assert rubric_res["overall_accuracy"] == 1.0


def test_compute_lafs():
    f1 = 1.0
    fast_lafs = compute_lafs(f1, latency_ms=10.0, tau=1000.0)
    slow_lafs = compute_lafs(f1, latency_ms=3000.0, tau=1000.0)
    assert fast_lafs > 0.98
    assert slow_lafs < 0.1


def test_calculate_latency_stats():
    lats = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    stats = calculate_latency_stats(lats)
    assert stats["mean"] == 55.0
    assert stats["p50"] == 60.0
    assert stats["p95"] == 100.0
    assert stats["max"] == 100.0


def test_db_manager_and_batch_indexing():
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_dir = Path(tmpdir)
        db_mgr = BenchmarkDBManager(cache_dir=cache_dir)

        sessions = [
            BenchmarkSession(
                session_id=f"sess_{i}",
                content=f"User likes topic {i} and works on neural memory models.",
                timestamp=f"2025-01-{i+1:02d}T10:00:00Z",
                tags=[f"tag_{i}"],
            )
            for i in range(5)
        ]

        # 1. Create fresh DB
        db_path, ingest_time, was_cached = db_mgr.get_or_create_db(
            suite_name="test_suite",
            sessions=sessions,
            use_cache=True,
            force_rebuild=True,
        )
        assert db_path.exists()
        assert not was_cached
        assert ingest_time >= 0.0

        # 2. Verify cached hit
        db_path2, ingest_time2, was_cached2 = db_mgr.get_or_create_db(
            suite_name="test_suite",
            sessions=sessions,
            use_cache=True,
            force_rebuild=False,
        )
        assert db_path2 == db_path
        assert was_cached2
        assert ingest_time2 == 0.0


def test_harness_adapter_error_handling():
    class FailingAdapter(BaseBenchmarkAdapter):
        name = "failing"
        version = "1.0"
        tenant_id = "fail"

        def load(self, limit: int | None = None):
            raise RuntimeError("Synthetic load failure")

    with tempfile.TemporaryDirectory() as tmpdir:
        harness = BenchmarkHarness(results_dir=Path(tmpdir))
        summary = harness.run_suite("failing", FailingAdapter())
        assert summary.total_questions == 0
        assert summary.error is not None
        assert "Synthetic load failure" in summary.error


def test_regression_checking():
    with tempfile.TemporaryDirectory() as tmpdir:
        baseline_file = Path(tmpdir) / "baseline.json"
        baseline_data = {
            "suites_evaluated": [
                {
                    "suite_name": "golden",
                    "macro_metrics": {"recall@10": 0.90, "mrr": 0.60},
                }
            ]
        }
        with open(baseline_file, "w", encoding="utf-8") as f:
            json.dump(baseline_data, f)

        # Passing case
        passing_summaries = [
            {
                "suite_name": "golden",
                "macro_metrics": {"recall@10": 0.88, "mrr": 0.58},
            }
        ]
        assert check_regression(passing_summaries, baseline_file, threshold=0.05)

        # Failing case
        failing_summaries = [
            {
                "suite_name": "golden",
                "macro_metrics": {"recall@10": 0.70, "mrr": 0.40},
            }
        ]
        assert not check_regression(failing_summaries, baseline_file, threshold=0.05)


def test_harness_cleanup_temp_dirs():
    with tempfile.TemporaryDirectory() as tmpdir:
        harness = BenchmarkHarness(results_dir=Path(tmpdir))
        sessions = [
            BenchmarkSession(
                session_id="s1",
                content="hello world",
                timestamp="2026-08-16T12:00:00Z",
            )
        ]
        # Ingest with use_cache=False to produce a temporary directory
        db_path, _, was_cached = harness.db_manager.get_or_create_db(
            suite_name="test_temp_cleanup",
            sessions=sessions,
            use_cache=False,
        )
        assert db_path.exists()
        assert not was_cached
        assert len(harness.db_manager._temp_dirs) > 0

        # Invoke cleanup
        harness.cleanup()
        assert len(harness.db_manager._temp_dirs) == 0
        assert not db_path.exists()


def test_rubric_prefix_stripping_and_matching():
    # Prompt-directive prefixes from benchmarks should be cleanly stripped
    rubric = [
        "LLM response should state: 17 tasks",
        "LLM response should state: 88%",
        "LLM response should contain: you explored various vector indexing strategies",
    ]
    pred = "The sprint on 2024-11-05 has 17 tasks logged in Jira with an 88% target. Earlier, you explored various vector indexing strategies."
    res = compute_text_metrics(pred, "17 tasks with 88% target", rubric=rubric)
    assert res["rubric_score"] == 1.0
    assert res["overall_accuracy"] == 1.0


def test_beam_date_and_multiplan_extraction():
    from eval.beam.run_beam_real import parse_time_anchor, extract_conversation_content

    # Date parsing
    assert "2024-07-01" in parse_time_anchor("July-01-2024")
    assert "2024-12-16" in parse_time_anchor("December-16-2024")
    assert "2025-02-15" in parse_time_anchor("February-15-2025")

    # Multi-plan extraction from nested chat structure
    dummy_chat = [
        {
            "plan-1": [
                {
                    "time_anchor": "July-01-2024",
                    "turns": [
                        [{"role": "user", "content": "hello plan 1", "id": 1}],
                    ],
                }
            ],
            "plan-2": None,
        },
        {
            "plan-1": None,
            "plan-2": [
                {
                    "time_anchor": "August-01-2024",
                    "turns": [
                        [{"role": "assistant", "content": "hello plan 2", "id": 2}],
                    ],
                }
            ],
        },
    ]

    turns = extract_conversation_content(dummy_chat)
    assert len(turns) == 2
    assert turns[0]["id"] == 1
    assert turns[0]["plan"] == "plan-1"
    assert turns[1]["id"] == 2
    assert turns[1]["plan"] == "plan-2"


