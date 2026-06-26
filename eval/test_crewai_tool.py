"""Tests for integrations.crewai.tool (AgenticMemorySearchTool + SaveTool).

Skip guard: crewai is not installable on Python 3.14 (tiktoken build failure).
Tests use unittest.skipUnless so the whole file is transparently skipped
in that environment.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

try:
    from crewai.tools import BaseTool
    from agentic_memory.integrations.crewai.tool import (
        AgenticMemorySearchTool,
        AgenticMemorySaveTool,
        AgenticMemorySearchInput,
        AgenticMemorySaveInput,
    )

    HAS_CREWAI = True
except ImportError:
    HAS_CREWAI = False

pytestmark = pytest.mark.skipif(
    not HAS_CREWAI,
    reason="crewai not installed (tiktoken build fails on Python 3.14) "
    "— pip install agentic-memory[crewai] on Python 3.11–3.13",
)


def _fresh_db(name: str) -> Path:
    p = Path(tempfile.mkdtemp(prefix=f"crewai_{name}_")) / "memory.db"
    os.environ["MEMORY_DB_PATH"] = str(p)
    return p


class TestAgenticMemorySearchTool(unittest.TestCase):
    def test_inherits_base_tool(self):
        assert issubclass(AgenticMemorySearchTool, BaseTool)

    def test_name(self):
        assert AgenticMemorySearchTool.name == "agentic_memory_search"

    def test_instantiable(self):
        t = AgenticMemorySearchTool(db_path=None)
        assert t is not None

    def test_run_returns_string(self):
        db = _fresh_db("search")
        os.environ["AGENTIC_MEMORY_DB_PATH"] = str(db)
        try:
            from agentic_memory import MemoryClient

            mc = MemoryClient(db_path=str(db))
            mc.save("CrewAI test memory", tags=["crew"], category="sdk")

            t = AgenticMemorySearchTool(db_path=str(db))
            result = t._run("CrewAI test")
            assert isinstance(result, str)
            assert "CrewAI test memory" in result
        finally:
            os.environ.pop("AGENTIC_MEMORY_DB_PATH", None)

    def test_run_no_results(self):
        db = _fresh_db("search_empty")
        t = AgenticMemorySearchTool(db_path=str(db))
        result = t._run("nothing_here_zzz")
        assert "[memory search: 0 results" in result


class TestAgenticMemorySaveTool(unittest.TestCase):
    def test_inherits_base_tool(self):
        assert issubclass(AgenticMemorySaveTool, BaseTool)

    def test_name(self):
        assert AgenticMemorySaveTool.name == "agentic_memory_save"

    def test_run_returns_note_id(self):
        db = _fresh_db("save")
        t = AgenticMemorySaveTool(db_path=str(db))
        result = t._run("Save this fact", tags=["fact"], category="decisions")
        assert isinstance(result, str)
        assert "Saved as sdk/" in result or "/" in result


class TestInputSchemas(unittest.TestCase):
    def test_search_input_default_limit(self):
        s = AgenticMemorySearchInput(query="test")
        assert s.limit == 5

    def test_save_input_defaults(self):
        s = AgenticMemorySaveInput(content="x")
        assert s.category == "sdk"
        assert s.tags == []


if __name__ == "__main__":
    unittest.main()
