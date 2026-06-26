"""Tests for integrations.langchain.history.AgenticMemoryChatHistory."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

try:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    from agentic_memory.integrations.langchain.history import (
        AgenticMemoryChatHistory,
        _role_tag,
    )

    HAS_LANGCHAIN = True
except ImportError:
    HAS_LANGCHAIN = False

pytestmark = pytest.mark.skipif(
    not HAS_LANGCHAIN,
    reason="langchain-core not installed",
)


def _fresh_db(name: str) -> Path:
    p = Path(tempfile.mkdtemp(prefix=f"history_{name}_")) / "memory.db"
    os.environ["MEMORY_DB_PATH"] = str(p)
    return p


class TestRoleTag(unittest.TestCase):
    def test_human_message(self):
        assert _role_tag(HumanMessage(content="hi")) == "human"

    def test_ai_message(self):
        assert _role_tag(AIMessage(content="hello")) == "ai"

    def test_system_message(self):
        assert _role_tag(SystemMessage(content="sys")) == "system"

    def test_unknown_type(self):
        class FakeMsg:
            content = "?"

        assert _role_tag(FakeMsg()) == "message"


class TestAgenticMemoryChatHistoryInit(unittest.TestCase):
    def test_defaults(self):
        h = AgenticMemoryChatHistory()
        assert h.db_path is None
        assert h.session_id == "default"
        assert h.messages == []

    def test_custom_args(self):
        h = AgenticMemoryChatHistory(db_path="/tmp/x.db", session_id="s1")
        assert h.db_path == "/tmp/x.db"
        assert h.session_id == "s1"


class TestAgenticMemoryChatHistoryAddMessage(unittest.TestCase):
    def test_human_message_saved_with_tags(self):
        db = _fresh_db("human")
        os.environ["AGENTIC_MEMORY_DB_PATH"] = str(db)
        try:
            h = AgenticMemoryChatHistory(db_path=str(db), session_id="sess-1")
            h.add_message(HumanMessage(content="What is 2+2?"))
            assert len(h.messages) == 1
        finally:
            os.environ.pop("AGENTIC_MEMORY_DB_PATH", None)

    def test_ai_message_saved_with_ai_tag(self):
        db = _fresh_db("ai")
        os.environ["AGENTIC_MEMORY_DB_PATH"] = str(db)
        try:
            h = AgenticMemoryChatHistory(db_path=str(db), session_id="sess-1")
            h.add_message(AIMessage(content="4"))
            assert len(h.messages) == 1
        finally:
            os.environ.pop("AGENTIC_MEMORY_DB_PATH", None)

    def test_messages_accumulate(self):
        db = _fresh_db("accumulate")
        os.environ["AGENTIC_MEMORY_DB_PATH"] = str(db)
        try:
            h = AgenticMemoryChatHistory(db_path=str(db), session_id="s1")
            h.add_message(HumanMessage(content="q1"))
            h.add_message(AIMessage(content="a1"))
            assert len(h.messages) == 2
        finally:
            os.environ.pop("AGENTIC_MEMORY_DB_PATH", None)


class TestAgenticMemoryChatHistoryClear(unittest.TestCase):
    def test_clear_empties_in_memory(self):
        db = _fresh_db("clear")
        h = AgenticMemoryChatHistory(db_path=str(db))
        h.add_message(HumanMessage(content="hello"))
        h.clear()
        assert h.messages == []


if __name__ == "__main__":
    unittest.main()
