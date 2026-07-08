"""Tests for integrations.crewai.memory.AgenticMemoryMemory.

Tests the v0 (save/search) and v1 (remember/recall) interfaces.

Skip guards:
- All tests in this file require crewai installed (skipped on Python 3.14
  because tiktoken does not build there).
- v1-specific tests additionally require crewai >= 1.0.
- The import of ``AgenticMemoryMemory`` itself does NOT require crewai;
  only the spec compliance and tool tests do.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

import pytest

try:
    from agentic_memory.integrations.crewai.memory import AgenticMemoryMemory

    HAS_AGENTIC_MEMORY = True
except ImportError:
    HAS_AGENTIC_MEMORY = False

try:
    import crewai as _crewai_mod

    _CREWAI_VERSION = getattr(_crewai_mod, "__version__", "0.0.0")
    _CREWAI_MAJOR = int(_CREWAI_VERSION.split(".")[0])
    HAS_CREWAI = True
except ImportError:
    _CREWAI_VERSION = "0.0.0"
    _CREWAI_MAJOR = 0
    HAS_CREWAI = False


def _fresh_db(name: str) -> Path:
    p = Path(tempfile.mkdtemp(prefix=f"crewai_mem_{name}_")) / "memory.db"
    os.environ["MEMORY_DB_PATH"] = str(p)
    return p


# ── pytestmarks ───────────────────────────────────────────────────────────

pytestmark = pytest.mark.skipif(
    not HAS_AGENTIC_MEMORY,
    reason="agentic-memory crewai integration not importable",
)

_skip_no_crewai = pytest.mark.skipif(
    not HAS_CREWAI,
    reason="crewai not installed — pip install on Python 3.11–3.13",
)

# ── Helpers ───────────────────────────────────────────────────────────────

_V1_MEMORY_KIND_VALUES = {"memory"}


def _assert_is_v1_memory_adapter(m: Any) -> None:
    assert hasattr(m, "memory_kind"), "AgenticMemoryMemory must have memory_kind field"
    assert m.memory_kind == "memory", (
        f"memory_kind must be 'memory' for CrewAI v1 discriminated union, got {m.memory_kind!r}"
    )
    assert hasattr(m, "remember"), "v1 adapter must have remember() method"
    assert hasattr(m, "recall"), "v1 adapter must have recall() method"
    assert hasattr(m, "drain_writes"), "v1 adapter must have drain_writes() method"
    assert callable(m.remember), "remember must be callable"
    assert callable(m.recall), "recall must be callable"
    assert callable(m.drain_writes), "drain_writes must be callable"


# ── Test classes ───────────────────────────────────────────────────────────


class TestAgenticMemoryMemoryInit(unittest.TestCase):
    """Construction and field defaults — no crewai required."""

    def test_instantiable(self):
        db = _fresh_db("init")
        m = AgenticMemoryMemory(db_path=str(db))
        assert m is not None

    def test_auto_tags_default(self):
        db = _fresh_db("tags")
        m = AgenticMemoryMemory(db_path=str(db))
        assert m.auto_tags == []

    def test_auto_tags_custom(self):
        db = _fresh_db("tags2")
        m = AgenticMemoryMemory(db_path=str(db), auto_tags=["crew-prod"])
        assert "crew-prod" in m.auto_tags

    @_skip_no_crewai
    def test_v1_memory_kind_field_present(self):
        """AgenticMemoryMemory must expose memory_kind='memory' for v1 discriminated union."""
        db = _fresh_db("v1kind")
        m = AgenticMemoryMemory(db_path=str(db))
        _assert_is_v1_memory_adapter(m)

    def test_read_only_default_false(self):
        db = _fresh_db("ro")
        m = AgenticMemoryMemory(db_path=str(db))
        assert m.read_only is False


class TestAgenticMemoryMemorySave(unittest.TestCase):
    """v0 .save() writes are retrievable."""

    def test_save_persists_content(self):
        db = _fresh_db("save")
        os.environ["AGENTIC_MEMORY_DB_PATH"] = str(db)
        try:
            m = AgenticMemoryMemory(db_path=str(db))
            m.save("User prefers dark mode", agent="researcher", task="research_ui")
            from agentic_memory import MemoryClient

            mc = MemoryClient(db_path=str(db))
            results = mc.search("dark mode", limit=5)
            assert results.total >= 1, (
                f"Expected at least 1 result, got {results.total}"
            )
            r = results.results[0]
            assert "crew" in r.tags
            assert "researcher" in r.tags
            assert "research_ui" in r.tags
        finally:
            os.environ.pop("AGENTIC_MEMORY_DB_PATH", None)

    def test_save_tags_agent_and_task(self):
        db = _fresh_db("tags")
        os.environ["AGENTIC_MEMORY_DB_PATH"] = str(db)
        try:
            m = AgenticMemoryMemory(db_path=str(db))
            m.save("some context", agent="writer", task="draft_post")
            from agentic_memory import MemoryClient

            mc = MemoryClient(db_path=str(db))
            results = mc.search("some context", limit=5)
            assert len(results.results) >= 1
            r = results.results[0]
            assert "crew" in r.tags
            assert "writer" in r.tags
            assert "draft_post" in r.tags
        finally:
            os.environ.pop("AGENTIC_MEMORY_DB_PATH", None)


class TestAgenticMemoryMemorySearch(unittest.TestCase):
    """v0 .search() returns list of dicts."""

    def test_search_returns_list_of_dicts(self):
        db = _fresh_db("search")
        os.environ["AGENTIC_MEMORY_DB_PATH"] = str(db)
        try:
            m = AgenticMemoryMemory(db_path=str(db))
            from agentic_memory import MemoryClient

            mc = MemoryClient(db_path=str(db))
            mc.save("some content to search for", tags=["k"], category="sdk")

            results = m.search("search for", limit=5)
            assert isinstance(results, list)
            assert len(results) >= 1
            assert "content" in results[0]
            assert "score" in results[0]
            assert "tags" in results[0]
        finally:
            os.environ.pop("AGENTIC_MEMORY_DB_PATH", None)

    def test_search_empty_db_returns_empty_list(self):
        db = _fresh_db("search_empty")
        m = AgenticMemoryMemory(db_path=str(db))
        results = m.search("nothing", limit=5)
        assert results == []


class TestAgenticMemoryMemoryV1(unittest.TestCase):
    """CrewAI v1 unified-memory protocol: remember/recall/recall with depth/filtering."""

    def setUp(self):
        if not HAS_CREWAI:
            self.skipTest("crewai not installed — pip install on Python 3.11–3.13")

    def test_remember_returns_record(self):
        db = _fresh_db("remember")
        os.environ["AGENTIC_MEMORY_DB_PATH"] = str(db)
        try:
            m = AgenticMemoryMemory(db_path=str(db))
            _assert_is_v1_memory_adapter(m)

            record = m.remember(
                "The user prefers dark mode",
                categories=["preferences", "ui"],
                importance=0.8,
            )
            assert record is not None
            assert record.content == "The user prefers dark mode"
            assert "preferences" in record.categories
            assert "ui" in record.categories
            assert record.importance == 0.8
        finally:
            os.environ.pop("AGENTIC_MEMORY_DB_PATH", None)

    def test_recall_returns_memory_matches(self):
        db = _fresh_db("recall")
        os.environ["AGENTIC_MEMORY_DB_PATH"] = str(db)
        try:
            m = AgenticMemoryMemory(db_path=str(db))
            m.remember("Python uses indentation for blocks", categories=["coding"])
            m.remember("User likes dark mode", categories=["preferences"])
            m.remember("FastAPI is async-first", categories=["coding"])

            matches = m.recall("dark mode preference", limit=5)
            assert isinstance(matches, list)
            assert len(matches) >= 1
            for match in matches:
                assert "record" in match
                assert "score" in match
                assert match["score"] >= 0.0
                assert match["record"]["content"]
        finally:
            os.environ.pop("AGENTIC_MEMORY_DB_PATH", None)

    def test_remember_and_recall_full_cycle(self):
        """Save via remember, retrieve via recall — the v1 save/search cycle."""
        db = _fresh_db("full_cycle")
        os.environ["AGENTIC_MEMORY_DB_PATH"] = str(db)
        try:
            m = AgenticMemoryMemory(db_path=str(db))

            # save three related items
            m.remember("Kubernetes is container orchestration", categories=["infra"])
            m.remember("Docker is container runtime", categories=["infra"])
            m.remember("Helm is K8s package manager", categories=["infra"])
            m.remember(
                "User's favourite colour is blue",
                categories=["personal"],
                importance=0.9,
            )

            # search for infra content: should match first three
            matches = m.recall("container orchestration", limit=5)
            assert len(matches) >= 1
            top_content = matches[0]["record"]["content"]
            infra_keywords = {"kubernetes", "docker", "helm", "container"}
            assert any(
                kw in top_content.lower()
                for kw in infra_keywords
            ), f"Expected infra content, got: {top_content}"

            # search for personal content: should find "favourite colour"
            personal_matches = m.recall("favourite colour", limit=5)
            assert len(personal_matches) >= 1
            assert "blue" in personal_matches[0]["record"]["content"].lower()
        finally:
            os.environ.pop("AGENTIC_MEMORY_DB_PATH", None)

    def test_drain_writes_is_noop(self):
        db = _fresh_db("drain")
        m = AgenticMemoryMemory(db_path=str(db))
        result = m.drain_writes()
        assert result is None

    def test_recall_empty_db_returns_empty(self):
        db = _fresh_db("recall_empty")
        m = AgenticMemoryMemory(db_path=str(db))
        matches = m.recall("nothing relevant", limit=5)
        assert matches == []

    def test_remember_with_agent_role_and_metadata(self):
        db = _fresh_db("with_agent")
        os.environ["AGENTIC_MEMORY_DB_PATH"] = str(db)
        try:
            m = AgenticMemoryMemory(db_path=str(db))
            record = m.remember(
                "Critical decision: use PostgreSQL for analytics",
                categories=["decisions", "database"],
                metadata={"project": "data-pipeline", "priority": "high"},
                agent_role="architect",
                importance=1.0,
            )
            assert record is not None
            assert record.metadata.get("project") == "data-pipeline"
            assert record.metadata.get("agent_role") == "architect"
            assert record.importance == 1.0
            assert "decisions" in record.categories
            assert "database" in record.categories
        finally:
            os.environ.pop("AGENTIC_MEMORY_DB_PATH", None)

    def test_read_only_skips_remember(self):
        db = _fresh_db("readonly")
        m = AgenticMemoryMemory(db_path=str(db), read_only=True)
        result = m.remember("should not be saved", categories=["test"])
        assert result is None


class TestAgenticMemoryMemoryBackwardCompat(unittest.TestCase):
    """v1 remember() is consistent with v0 save(); v1 recall() consistent with v0 search()."""

    def test_save_delegates_to_remember(self):
        db = _fresh_db("delegate")
        os.environ["AGENTIC_MEMORY_DB_PATH"] = str(db)
        try:
            m = AgenticMemoryMemory(db_path=str(db))
            note_id = m.save("legacy save", agent="a", task="t")
            assert note_id
            from agentic_memory import MemoryClient

            mc = MemoryClient(db_path=str(db))
            results = mc.search("legacy", limit=5)
            assert results.total >= 1
        finally:
            os.environ.pop("AGENTIC_MEMORY_DB_PATH", None)

    def test_search_delegates_to_recall(self):
        db = _fresh_db("delegate2")
        os.environ["AGENTIC_MEMORY_DB_PATH"] = str(db)
        try:
            m = AgenticMemoryMemory(db_path=str(db))
            m.remember("delegate search test", categories=["test"])
            v0_results = m.search("delegate search", limit=5)
            assert isinstance(v0_results, list)
            assert len(v0_results) >= 1
        finally:
            os.environ.pop("AGENTIC_MEMORY_DB_PATH", None)


if __name__ == "__main__":
    unittest.main()
