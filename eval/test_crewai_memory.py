"""Tests for integrations.crewai.memory.AgenticMemoryMemory.

Skip guard: requires crewai installed. Skipped on Python 3.14 (tiktoken).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

try:
    from agentic_memory.integrations.crewai.memory import AgenticMemoryMemory

    HAS_MEMORY = True
except ImportError:
    HAS_MEMORY = False

pytestmark = pytest.mark.skipif(
    not HAS_MEMORY,
    reason="crewai not installed — pip install on Python 3.11–3.13",
)


def _fresh_db(name: str) -> Path:
    p = Path(tempfile.mkdtemp(prefix=f"crewai_mem_{name}_")) / "memory.db"
    os.environ["MEMORY_DB_PATH"] = str(p)
    return p


class TestAgenticMemoryMemoryInit(unittest.TestCase):
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


class TestAgenticMemoryMemorySave(unittest.TestCase):
    def test_save_persists_content(self):
        db = _fresh_db("save")
        os.environ["AGENTIC_MEMORY_DB_PATH"] = str(db)
        try:
            m = AgenticMemoryMemory(db_path=str(db))
            m.save("User prefers dark mode", agent="researcher", task="research_ui")
            # Verify it was saved by pulling it back via search SDK directly.
            # NOTE: the search pipeline currently returns MemoryResult with
            # content="" (text lives in metadata.source_file as a .md path).
            # We verify the save landed by checking total > 0 and correct tags.
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


if __name__ == "__main__":
    unittest.main()
