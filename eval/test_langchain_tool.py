"""Tests for integrations.langchain.tool — _format_as_llm_readable + schema."""

from __future__ import annotations

import sys
import unittest

import pytest

try:
    from langchain_core.tools import StructuredTool
    from agentic_memory.integrations.langchain.tool import (
        _format_as_llm_readable,
        SearchMemoryInput,
        SaveMemoryInput,
        search_tool,
        save_tool,
    )

    HAS_LANGCHAIN = True
except ImportError:
    HAS_LANGCHAIN = False

pytestmark = pytest.mark.skipif(
    not HAS_LANGCHAIN,
    reason="langchain-core not installed — pip install agentic-memory[langchain]",
)


# ── Stubs ─────────────────────────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, content, score=0.5, tags=None, category=""):
        self.content = content
        self.score = score
        self.tags = tags or []
        self.category = category


class _FakeSearchResults:
    def __init__(self, results, total=0, query="", synthesis=""):
        self.results = results
        self.total = total if total else len(results)
        self.query = query
        self.synthesis = synthesis


# ── _format_as_llm_readable ───────────────────────────────────────────────────


class TestFormatAsLlmReadable(unittest.TestCase):
    def test_empty_results(self):
        r = _format_as_llm_readable(_FakeSearchResults([]))
        assert "[memory search: 0 results" in r

    def test_single_result(self):
        r = _format_as_llm_readable(
            _FakeSearchResults([_FakeResult("hello", score=0.9)], query="greet")
        )
        assert "hello" in r
        assert "score=0.900" in r

    def test_tags_included(self):
        r = _format_as_llm_readable(
            _FakeSearchResults([_FakeResult("x", tags=["a", "b"])])
        )
        assert "a, b" in r

    def test_synthesis_appended(self):
        r = _format_as_llm_readable(
            _FakeSearchResults([_FakeResult("x")], synthesis="summary here")
        )
        assert "[synthesis]" in r
        assert "summary here" in r

    def test_no_synthesis_no_heading(self):
        r = _format_as_llm_readable(_FakeSearchResults([_FakeResult("x")]))
        assert "[synthesis]" not in r


# ── Input schemas ─────────────────────────────────────────────────────────────


class TestSearchMemoryInput(unittest.TestCase):
    def test_default_limit(self):
        s = SearchMemoryInput(query="test")
        assert s.limit == 5

    def test_custom_limit(self):
        s = SearchMemoryInput(query="test", limit=10)
        assert s.limit == 10

    def test_limit_max(self):
        s = SearchMemoryInput(query="test", limit=50)
        assert s.limit == 50

    def test_limit_above_max_raises(self):
        with pytest.raises(Exception):
            SearchMemoryInput(query="test", limit=51)


class TestSaveMemoryInput(unittest.TestCase):
    def test_defaults(self):
        s = SaveMemoryInput(content="hello")
        assert s.category == "sdk"
        # tags: list[str] | None with default=None → None (Pydantic v2 behaviour)
        assert s.tags is None

    def test_custom_category(self):
        s = SaveMemoryInput(content="x", category="decisions")
        assert s.category == "decisions"

    def test_custom_tags(self):
        s = SaveMemoryInput(content="x", tags=["a", "b"])
        assert s.tags == ["a", "b"]


# ── Tool constructions ────────────────────────────────────────────────────────


class TestToolConstructors(unittest.TestCase):
    def test_search_tool_has_invoke(self):
        assert hasattr(search_tool, "invoke") or hasattr(search_tool, "run")

    def test_save_tool_has_invoke(self):
        assert hasattr(save_tool, "invoke") or hasattr(save_tool, "run")

    def test_search_tool_name(self):
        assert "search" in search_tool.name

    def test_save_tool_name(self):
        assert "save" in save_tool.name


if __name__ == "__main__":
    unittest.main()
