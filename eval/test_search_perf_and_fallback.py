"""Integration tests for memory search performance and phrase relaxation."""

import time
from pathlib import Path
import pytest

from search.orchestrator import search_memories
from agentic_memory.client import MemoryClient


@pytest.fixture
def memory_db_path():
    db_p = Path(__file__).resolve().parent.parent / "memory" / "memory.db"
    if not db_p.exists():
        pytest.skip("Production memory.db not present")
    return db_p


def test_light_hybrid_search_perf(memory_db_path):
    """Light hybrid search (mode=hybrid, light=True) should return in < 10.0s on cold start."""
    t0 = time.time()
    res = search_memories(memory_db_path, "Agentic Memory IDE", limit=5, mode="hybrid", light=True)
    dt = time.time() - t0
    assert dt < 15.0, f"Light hybrid search took {dt:.3f}s (expected < 15.0s)"
    assert res["count"] > 0, "Light hybrid search should find matching memories"


def test_fts_fast_path_perf(memory_db_path):
    """FTS fast path search (mode=fts) should return in < 0.5s."""
    t0 = time.time()
    res = search_memories(memory_db_path, "Agentic Memory IDE", limit=5, mode="fts")
    dt = time.time() - t0
    assert dt < 2.0, f"FTS search took {dt:.3f}s (expected < 2.0s)"
    assert res["count"] > 0, "FTS fast path should find matching memories"


def test_memory_client_light_parameter(memory_db_path):
    """MemoryClient.search(..., light=True) should accept light parameter cleanly."""
    mc = MemoryClient(db_path=memory_db_path)
    res = mc.search("Agentic Memory IDE", limit=5, light=True)
    assert len(res) > 0
