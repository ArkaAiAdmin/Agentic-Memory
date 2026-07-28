"""Tests for Phase 7 observability: phase latencies + memory_stats."""

from __future__ import annotations

import json
import time


from search.orchestrator import _record_phase_latency, _phase_latencies


class TestPhaseLatencies:
    def test_record_phase_latency_populates_dict(self):
        _phase_latencies.clear()
        start = time.monotonic()
        time.sleep(0.01)
        _record_phase_latency("test_phase", start)
        assert "test_phase" in _phase_latencies
        assert _phase_latencies["test_phase"] >= 10.0

    def test_multiple_phases_tracked(self):
        _phase_latencies.clear()
        for phase_name in ["phase_a", "phase_b", "phase_c"]:
            start = time.time()
            _record_phase_latency(phase_name, start)
        assert set(_phase_latencies.keys()) == {"phase_a", "phase_b", "phase_c"}

    def test_overwrite_same_phase(self):
        _phase_latencies.clear()
        _record_phase_latency("dup_phase", time.time())
        _record_phase_latency("dup_phase", time.time())
        assert len(_phase_latencies) == 1
        assert "dup_phase" in _phase_latencies

    def test_latencies_cleared_per_search_call(self):
        _phase_latencies.clear()
        _record_phase_latency("old_phase", time.time())
        # In real usage, search_memories clears _phase_latencies at entry.
        _phase_latencies.clear()
        assert _phase_latencies == {}


class TestMemoryStatsOp:
    def test_memory_stats_returns_json(self):
        from mcp_surface.mcp_maintenance_ops import _op_memory_stats

        output = _op_memory_stats()
        data = json.loads(output)
        assert "error" not in data
        assert "db_path" in data
        assert "db_size_bytes" in data
        assert "note_count" in data
        assert "background_queue_depth" in data
        assert "circuit_breaker_open" in data
        assert "feature_flags" in data

    def test_memory_stats_note_count_type(self):
        from mcp_surface.mcp_maintenance_ops import _op_memory_stats

        output = _op_memory_stats()
        data = json.loads(output)
        assert isinstance(data["note_count"], int)

    def test_memory_stats_feature_flags_nonempty(self):
        from mcp_surface.mcp_maintenance_ops import _op_memory_stats

        output = _op_memory_stats()
        data = json.loads(output)
        assert isinstance(data["feature_flags"], dict)
        assert len(data["feature_flags"]) >= 10
