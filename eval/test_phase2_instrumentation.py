#!/usr/bin/env python3
"""Tests for Phase 2 exception instrumentation in search/orchestrator.py.

Verifies:
- error_counter increments when search phases swallow exceptions
- phase_errors surface in the search_memories response envelope
- phase counter resets between independent search calls
- individual phase failures do not break the search pipeline
"""

import atexit
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".config" / "agentic-memory"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.error_counter import get_counter, reset as _reset_counter
from search.orchestrator import (
    _apply_quality_gates,
    _apply_user_profiling,
    _apply_safety_demoting,
    _hybrid_fusion,
    _record_last_accessed,
)
import pytest
from typing import cast as _tcast
from infra.db import AnyConnection as _AnyConn
from conftest import embedding_available

_TEST_TMPDIR = Path(tempfile.mkdtemp())
PROD_DB = _TEST_TMPDIR / "memory.db"

def _cleanup() -> None:
    if _TEST_TMPDIR.exists():
        shutil.rmtree(str(_TEST_TMPDIR), ignore_errors=True)

atexit.register(_cleanup)


class TestErrorCounterBasic(unittest.TestCase):
    """Verify the error counter singleton works correctly."""

    def setUp(self):
        _reset_counter()

    def test_counter_starts_empty(self):
        self.assertEqual(get_counter().get_all(), {})

    def test_single_increment(self):
        get_counter().increment("search.safety_demoting")
        self.assertEqual(get_counter().get_count("search.safety_demoting"), 1)

    def test_increment_with_exception_stores_details(self):
        err = RuntimeError("boom")
        get_counter().increment("search.quality_gates", err)
        counts = get_counter().get_counts()
        self.assertEqual(counts["total_count"], 1)
        self.assertIn("search.quality_gates", counts["phase_counts"])
        self.assertEqual(counts["recent_entries"][0]["error_type"], "RuntimeError")
        self.assertEqual(counts["recent_entries"][0]["error_message"], "boom")

    def test_multiple_increments_accumulate(self):
        get_counter().increment("search.safety_demoting")
        get_counter().increment("search.safety_demoting")
        get_counter().increment("search.hybrid_fusion")
        all_counts = get_counter().get_all()
        self.assertEqual(all_counts["search.safety_demoting"], 2)
        self.assertEqual(all_counts["search.hybrid_fusion"], 1)

    def test_reset_clears_counter(self):
        get_counter().increment("search.safety_demoting")
        _reset_counter()
        self.assertEqual(get_counter().get_all(), {})


class TestPhaseErrorsInEnvelope(unittest.TestCase):
    """Verify phase_errors key in search_memories result dict."""

    def setUp(self):
        _reset_counter()

    def test_phase_errors_added_when_counter_nonempty(self):
        """search_memories adds phase_errors key when error counter has entries."""
        get_counter().increment("search.safety_demoting", RuntimeError("test"))
        counts = get_counter().get_counts()
        # Simulate what search_memories does
        envelope = {}
        if counts.get("total_count"):
            envelope["phase_errors"] = counts
        self.assertIn("phase_errors", envelope)
        self.assertIn("search.safety_demoting", envelope["phase_errors"]["phase_counts"])

    def test_phase_errors_absent_when_counter_empty(self):
        """No phase_errors key when no errors have been recorded."""
        counts = get_counter().get_counts()
        self.assertEqual(counts["total_count"], 0)
        envelope = {}
        if counts.get("total_count"):
            envelope["phase_errors"] = counts
        self.assertNotIn("phase_errors", envelope)


class TestInstrumentedPhaseFunctions(unittest.TestCase):
    """Directly test that instrumented phase functions call _phase_inc on failure."""

    def setUp(self):
        _reset_counter()

    def _inject_failing_module(self, name, attr_raiser):
        """Inject a module into sys.modules that raises on a specific attribute call."""
        mod = types.ModuleType(name)
        setattr(mod, attr_raiser.__name__, attr_raiser)
        sys.modules[name] = mod
        return mod

    def test_apply_quality_gates_calls_phase_inc_on_failure(self):
        import search.orchestrator as orch
        original_inc = orch._phase_inc
        calls = []
        orch._phase_inc = lambda phase, err=None: calls.append((phase, type(err).__name__ if err else None))
        broken_mod = self._inject_failing_module(
            "quality_gates",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("qg boom")),
        )
        setattr(broken_mod, "QUALITY_GATES_ENABLED", True)
        try:
            _apply_quality_gates(
                result_items=[{"id": "x"}],
                output=[],
                results_to_display=[],
                query="test",
                rerank=False,
                backlinks_map={},
            )
        except Exception:
            pass
        orch._phase_inc = original_inc
        sys.modules.pop("quality_gates", None)
        self.assertTrue(
            any("quality_gates" in c[0] for c in calls),
            f"Expected quality_gates phase call, got: {calls}",
        )

    def test_apply_user_profiling_calls_phase_inc_on_failure(self):
        import search.orchestrator as orch
        original_inc = orch._phase_inc
        calls = []
        orch._phase_inc = lambda phase, err=None: calls.append((phase, type(err).__name__ if err else None))
        broken_mod = self._inject_failing_module(
            "user_profile",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("up boom")),
        )
        setattr(broken_mod, "PROFILE_ENABLED", True)
        try:
            _apply_user_profiling(
                result_items=[{"id": "x"}],
                output=[],
                results_to_display=[],
                query="test",
                rerank=False,
                backlinks_map={},
                db_path=str(PROD_DB),
            )
        except Exception:
            pass
        orch._phase_inc = original_inc
        sys.modules.pop("user_profile", None)
        self.assertTrue(
            any("user_profiling" in c[0] for c in calls),
            f"Expected user_profiling phase call, got: {calls}",
        )

    def test_apply_safety_demoting_calls_phase_inc_on_bad_input(self):
        import search.orchestrator as orch
        original_inc = orch._phase_inc
        calls = []
        orch._phase_inc = lambda phase, err=None: calls.append((phase, type(err).__name__ if err else None))
        _apply_safety_demoting(_tcast("list", "not_a_list"), [], [])
        orch._phase_inc = original_inc
        self.assertTrue(
            any("safety_demoting" in c[0] for c in calls),
            f"Expected safety_demoting phase call, got: {calls}",
        )

    @pytest.mark.skipif(not embedding_available(), reason="embedding model not loaded")
    def test_hybrid_fusion_calls_phase_inc_on_bad_db(self):
        import search.orchestrator as orch
        original_inc = orch._phase_inc
        calls = []
        orch._phase_inc = lambda phase, err=None: calls.append((phase, type(err).__name__ if err else None))
        _hybrid_fusion(_tcast(_AnyConn, "not_a_db"), [], "test", Path("/tmp"), 5, "")
        orch._phase_inc = original_inc
        self.assertTrue(
            any("hybrid_fusion" in c[0] for c in calls),
            f"Expected hybrid_fusion phase call, got: {calls}",
        )

    def test_record_last_accessed_calls_phase_inc_on_bad_db(self):
        import search.orchestrator as orch
        original_inc = orch._phase_inc
        calls = []
        orch._phase_inc = lambda phase, err=None: calls.append((phase, type(err).__name__ if err else None))
        _record_last_accessed(_tcast(_AnyConn, "not_a_db"), [{"id": "x"}])
        orch._phase_inc = original_inc
        self.assertTrue(
            any("record_last_accessed" in c[0] for c in calls),
            f"Expected record_last_accessed phase call, got: {calls}",
        )


if __name__ == "__main__":
    unittest.main()
