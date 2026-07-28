"""Tests for budget-aware search cascade (Phase 7).

Covers:
  - Adaptive overfetch: scaling with corpus size and query type
  - SearchBudget: stage gating, timing, envelope reporting
  - Integration: budget passed through search pipeline
"""

from __future__ import annotations

from unittest import mock



class TestAdaptiveOverfetch:
    def test_small_corpus(self):
        from search.budget_aware import compute_adaptive_overfetch
        # Small corpus: no overfetch needed
        overfetch = compute_adaptive_overfetch(100, base_overfetch=3)
        assert 1 <= overfetch <= 5

    def test_large_corpus(self):
        from search.budget_aware import compute_adaptive_overfetch
        # Large corpus: more overfetch
        small = compute_adaptive_overfetch(100, base_overfetch=3)
        large = compute_adaptive_overfetch(100000, base_overfetch=3)
        assert large >= small

    def test_logarithmic_scaling(self):
        from search.budget_aware import compute_adaptive_overfetch
        # Should scale with log10
        o1k = compute_adaptive_overfetch(1000, base_overfetch=3)
        o100k = compute_adaptive_overfetch(100000, base_overfetch=3)
        o1m = compute_adaptive_overfetch(1000000, base_overfetch=3)
        # Each should be >= previous
        assert o1k <= o100k <= o1m

    def test_code_query_less_overfetch(self):
        from search.budget_aware import compute_adaptive_overfetch
        base = compute_adaptive_overfetch(10000, base_overfetch=3, query_type="general")
        code = compute_adaptive_overfetch(10000, base_overfetch=3, query_type="code")
        assert code <= base

    def test_multihop_more_overfetch(self):
        from search.budget_aware import compute_adaptive_overfetch
        base = compute_adaptive_overfetch(10000, base_overfetch=3, query_type="general")
        multihop = compute_adaptive_overfetch(10000, base_overfetch=3, query_type="multihop")
        assert multihop >= base

    def test_clamped_range(self):
        from search.budget_aware import compute_adaptive_overfetch
        for size in [1, 10, 100, 1000, 1000000]:
            overfetch = compute_adaptive_overfetch(size, base_overfetch=3)
            assert 1 <= overfetch <= 10

    def test_zero_corpus(self):
        from search.budget_aware import compute_adaptive_overfetch
        overfetch = compute_adaptive_overfetch(0, base_overfetch=3)
        assert overfetch == 3  # Returns base


class TestSearchBudget:
    def test_unlimited_budget(self):
        from search.budget_aware import SearchBudget
        budget = SearchBudget(budget_ms=0)
        assert budget.remaining_ms == 0  # 0 means unlimited
        assert budget.should_run("any_stage", 1000) is True

    def test_stage_gating(self):
        from search.budget_aware import SearchBudget
        budget = SearchBudget(budget_ms=100)
        # Should run cheap stage
        assert budget.should_run("fts", 10) is True
        # Should skip expensive stage
        assert budget.should_run("colbert", 200) is False

    def test_stages_tracked(self):
        from search.budget_aware import SearchBudget
        budget = SearchBudget(budget_ms=100)
        budget.should_run("fts", 10)
        budget.should_run("colbert", 200)
        budget.should_run("answer_rerank", 50)
        assert "fts" in budget.stages_run
        assert "colbert" in budget.stages_skipped
        assert "answer_rerank" in budget.stages_run  # 50 < 90 remaining

    def test_envelope_dict(self):
        from search.budget_aware import SearchBudget
        budget = SearchBudget(budget_ms=200)
        budget.should_run("fts", 10)
        d = budget.to_dict()
        assert "budget_ms" in d
        assert "elapsed_ms" in d
        assert "stages_run" in d
        assert "stages_skipped" in d
        assert d["budget_ms"] == 200

    def test_env_var_parsing(self):
        from search.budget_aware import get_search_budget
        import os
        # Test with env var set
        os.environ["MEMORY_SEARCH_COMPUTE_BUDGET_MS"] = "150"
        try:
            budget = get_search_budget()
            assert budget.budget_ms == 150
        finally:
            del os.environ["MEMORY_SEARCH_COMPUTE_BUDGET_MS"]

    def test_env_var_missing(self):
        from search.budget_aware import get_search_budget
        import os
        os.environ.pop("MEMORY_SEARCH_COMPUTE_BUDGET_MS", None)
        budget = get_search_budget()
        assert budget.budget_ms == 200.0  # Default from memory.toml

    def test_env_var_invalid(self):
        from search.budget_aware import get_search_budget
        import os
        os.environ["MEMORY_SEARCH_COMPUTE_BUDGET_MS"] = "not_a_number"
        try:
            budget = get_search_budget()
            assert budget.budget_ms == 200.0  # Falls back to memory.toml default
        finally:
            del os.environ["MEMORY_SEARCH_COMPUTE_BUDGET_MS"]


class TestBudgetCascade:
    def test_tight_budget_skips_when_real_time_accumulates(self):
        from search.budget_aware import SearchBudget
        # Simulate a budget that expires partway through the pipeline
        with mock.patch("time.time", return_value=1000.0):
            budget = SearchBudget(budget_ms=30, start_time=1000.0)
            # FTS runs (elapsed = 0)
            assert budget.should_run("fts", 5) is True
        # Advance time by 40ms — exceeds 30ms budget
        with mock.patch("time.time", return_value=1000.04):
            assert budget.should_run("semantic", 50) is False
            assert budget.should_run("chunk_ce", 100) is False
            assert budget.should_run("colbert", 100) is False

    def test_no_budget_runs_all(self):
        from search.budget_aware import SearchBudget
        budget = SearchBudget(budget_ms=0)  # Unlimited
        assert budget.should_run("fts", 5) is True
        assert budget.should_run("semantic", 50) is True
        assert budget.should_run("chunk_ce", 100) is True
        assert budget.should_run("colbert", 100) is True
        assert budget.should_run("answer_rerank", 50) is True
        assert budget.stages_skipped == []

    def test_budget_tracks_elapsed(self):
        from search.budget_aware import SearchBudget
        budget = SearchBudget(budget_ms=100, start_time=1000.0)
        with mock.patch("time.time", return_value=1000.01):
            assert budget.elapsed_ms > 0
            assert budget.remaining_ms < 100
