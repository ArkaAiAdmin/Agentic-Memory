"""Tests for real-memory golden evaluation harness (Phase 8).

Covers:
  - Golden set loading and validation
  - Metric computation (recall@k, MRR)
  - DB setup and memory insertion
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest


class TestGoldenSet:
    def test_loadsSuccessfully(self):
        from eval.real_memory_eval import _load_golden_set
        golden = _load_golden_set()
        assert "memories" in golden
        assert "test_cases" in golden
        assert "targets" in golden

    def test_has_100_memories(self):
        from eval.real_memory_eval import _load_golden_set
        golden = _load_golden_set()
        assert len(golden["memories"]) >= 90  # At least 90 memories

    def test_has_100_test_cases(self):
        from eval.real_memory_eval import _load_golden_set
        golden = _load_golden_set()
        assert len(golden["test_cases"]) >= 90  # At least 90 test cases

    def test_test_cases_have_expected(self):
        from eval.real_memory_eval import _load_golden_set
        golden = _load_golden_set()
        for tc in golden["test_cases"]:
            assert "query" in tc
            assert "expected" in tc
            assert len(tc["expected"]) > 0

    def test_targets_defined(self):
        from eval.real_memory_eval import _load_golden_set
        golden = _load_golden_set()
        targets = golden["targets"]
        assert "recall_at_10" in targets
        assert "mrr" in targets
        assert "p95_cold_latency_ms" in targets
        assert "p95_warm_latency_ms" in targets


class TestMetrics:
    def test_recall_at_k_perfect(self):
        from eval.real_memory_eval import _compute_recall_at_k
        retrieved = ["a", "b", "c", "d", "e"]
        expected = ["a", "b"]
        assert _compute_recall_at_k(retrieved, expected, k=10) == 1.0

    def test_recall_at_k_partial(self):
        from eval.real_memory_eval import _compute_recall_at_k
        retrieved = ["a", "x", "y", "z"]
        expected = ["a", "b"]
        assert _compute_recall_at_k(retrieved, expected, k=10) == 0.5

    def test_recall_at_k_none_found(self):
        from eval.real_memory_eval import _compute_recall_at_k
        retrieved = ["x", "y", "z"]
        expected = ["a", "b"]
        assert _compute_recall_at_k(retrieved, expected, k=10) == 0.0

    def test_recall_at_k_empty_expected(self):
        from eval.real_memory_eval import _compute_recall_at_k
        retrieved = ["a", "b"]
        assert _compute_recall_at_k(retrieved, [], k=10) == 1.0

    def test_mrr_first_position(self):
        from eval.real_memory_eval import _compute_mrr
        assert _compute_mrr(["a", "b", "c"], ["a"]) == 1.0

    def test_mrr_second_position(self):
        from eval.real_memory_eval import _compute_mrr
        assert _compute_mrr(["x", "a", "b"], ["a"]) == 0.5

    def test_mrr_not_found(self):
        from eval.real_memory_eval import _compute_mrr
        assert _compute_mrr(["x", "y", "z"], ["a"]) == 0.0

    def test_mrr_multiple_expected(self):
        from eval.real_memory_eval import _compute_mrr
        # Returns reciprocal rank of FIRST match
        assert _compute_mrr(["x", "a", "b"], ["a", "b"]) == 0.5


class TestDBSetup:
    def test_setup_creates_schema(self):
        from eval.real_memory_eval import _setup_db
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = _setup_db(db_path)
            # Check memories table exists
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = [t[0] for t in tables]
            assert "memories" in table_names
            conn.close()

    def test_insert_memories(self):
        from eval.real_memory_eval import _setup_db, _insert_memories
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = _setup_db(db_path)
            memories = [
                {"note_id": "test/mem1", "content": "Test content", "tags": ["test"], "category": "lessons"},
                {"note_id": "test/mem2", "content": "Another content", "tags": ["test"], "category": "decisions"},
            ]
            _insert_memories(conn, memories)
            count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            assert count == 2
            conn.close()
