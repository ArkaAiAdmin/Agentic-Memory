"""Shared tests for integrations — _format_as_llm_readable.

Requires at least one of langchain or crewai formatter to be importable.
"""

from __future__ import annotations

import sys
import unittest
from typing import Any

import pytest


# ── Formatter discovery ───────────────────────────────────────────────────────

HAVE_FORMATTERS: list[tuple[str, Any]] = []

try:
    from agentic_memory.integrations.langchain.tool import (
        _format_as_llm_readable as lc_fmt,
    )

    HAVE_FORMATTERS.append(("langchain", lc_fmt))
except ImportError:
    pass

try:
    from agentic_memory.integrations.crewai.tool import (
        _format_as_llm_readable as crew_fmt,
    )

    HAVE_FORMATTERS.append(("crewai", crew_fmt))
except ImportError:
    pass


# ── Stubs ─────────────────────────────────────────────────────────────────────


class _R:
    def __init__(self, content, score=0.5, tags=None, category=""):
        self.content = content
        self.score = score
        self.tags = tags or []
        self.category = category


class _SR:
    def __init__(self, results, total=0, query="", synthesis=""):
        self.results = results
        self.total = total if total else len(results)
        self.query = query
        self.synthesis = synthesis


# ── One test class per formatter ──────────────────────────────────────────────


def _make_test_class(name: str, fmt):
    class _T(unittest.TestCase):
        integrator_name = name  # for pytest id() if needed

        def test_empty_results(self):
            r = fmt(_SR([]))
            assert "[memory search: 0 results" in r

        def test_includes_query(self):
            r = fmt(_SR([_R("x")], query="my query"))
            assert "my query" in r

        def test_score_formatting(self):
            r = fmt(_SR([_R("x", score=0.1234)]))
            assert "0.123" in r  # 3 decimal places

        def test_tags_printed(self):
            r = fmt(_SR([_R("x", tags=["alpha", "beta"])]))
            assert "alpha" in r
            assert "beta" in r

        def test_no_tags_omits_tag_line(self):
            r = fmt(_SR([_R("x", tags=[])]))
            assert "tags:" not in r

        def test_synthesis_appended(self):
            r = fmt(_SR([_R("x")], synthesis="synthesis text"))
            assert "[synthesis]" in r
            assert "synthesis text" in r

        def test_no_synthesis_heading_absent(self):
            r = fmt(_SR([_R("x")]))
            assert "[synthesis]" not in r

        def test_multiple_results_numbered(self):
            r = fmt(_SR([_R("first"), _R("second"), _R("third")], query="q"))
            assert "1." in r
            assert "2." in r
            assert "3." in r

    _T.__name__ = f"TestFormatAsLlmReadable[{name}]"
    return _T


for _name, _fmt in HAVE_FORMATTERS:
    globals()[f"TestFormatAsLlmReadable_{_name}"] = _make_test_class(_name, _fmt)


class TestAtLeastOneFormatter(unittest.TestCase):
    def test_at_least_one_available(self):
        assert len(HAVE_FORMATTERS) >= 1, (
            "Neither langchain nor crewai formatter importable — "
            "install at least one extras group."
        )


if __name__ == "__main__":
    unittest.main()
