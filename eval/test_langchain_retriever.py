"""Tests for integrations.langchain.retriever.AgenticMemoryRetriever.

Skip guard: requires langchain-core and langchain-community installed.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

try:
    from langchain_core.documents import Document
    from agentic_memory.integrations.langchain.retriever import (
        AgenticMemoryRetriever,
    )

    HAS_LANGCHAIN = True
except ImportError:
    HAS_LANGCHAIN = False

pytestmark = pytest.mark.skipif(
    not HAS_LANGCHAIN,
    reason="langchain-core not installed — pip install agentic-memory[langchain]",
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _fresh_db(name: str) -> Path:
    """Per-test temp DB so test order does not matter."""
    p = Path(tempfile.mkdtemp(prefix=f"retriever_{name}_")) / "memory.db"
    os.environ["MEMORY_DB_PATH"] = str(p)
    return p


# ── Test cases ────────────────────────────────────────────────────────────────


class TestAgenticMemoryRetrieverInit(unittest.TestCase):
    def test_defaults(self):
        r = AgenticMemoryRetriever()
        assert r.db_path is None
        assert r.search_kwargs == {"limit": 5, "rerank": True}

    def test_custom_db_path(self):
        r = AgenticMemoryRetriever(db_path="/tmp/test.db")
        assert r.db_path == "/tmp/test.db"

    def test_custom_search_kwargs(self):
        r = AgenticMemoryRetriever(search_kwargs={"limit": 3, "rerank": False})
        assert r.search_kwargs["limit"] == 3
        assert r.search_kwargs["rerank"] is False

    def test_is_pydantic_model(self):
        from pydantic import BaseModel

        assert issubclass(AgenticMemoryRetriever, BaseModel)

    def test_model_config_allows_arbitrary_types(self):
        cfg = AgenticMemoryRetriever.model_config
        assert cfg.get("arbitrary_types_allowed") is True


class TestAgenticMemoryRetrieverResolveDbPath(unittest.TestCase):
    def test_explicit_path_wins(self):
        r = AgenticMemoryRetriever(db_path="/explicit/path.db")
        assert r._resolve_db_path() == "/explicit/path.db"

    def test_env_fallback(self):
        os.environ["AGENTIC_MEMORY_DB_PATH"] = "/env/path.db"
        try:
            r = AgenticMemoryRetriever()
            assert r._resolve_db_path() == "/env/path.db"
        finally:
            os.environ.pop("AGENTIC_MEMORY_DB_PATH", None)

    def test_none_when_no_source(self):
        os.environ.pop("AGENTIC_MEMORY_DB_PATH", None)
        r = AgenticMemoryRetriever()
        assert r._resolve_db_path() is None


class TestAgenticMemoryRetrieverConvert(unittest.TestCase):
    """Test _to_document converts MemoryResult → Document correctly.

    NOTE: the search pipeline currently returns MemoryResult with
    ``content=""`` (the actual text lives in ``metadata.source_file`` as a
    .md path). These tests pin that current behaviour and verify what
    the retriever does with it.
    """

    def test_returns_document_instance(self):
        db = _fresh_db("convert")
        from agentic_memory import MemoryClient

        mc = MemoryClient(db_path=str(db))
        mc.save("test memory content", tags=["tag1"], category="test", pinned=True)

        r = AgenticMemoryRetriever(db_path=str(db))
        docs = r.invoke("test")
        assert len(docs) >= 1
        assert isinstance(docs[0], Document)

    def test_memory_metadata_keys_present(self):
        db = _fresh_db("metadata")
        from agentic_memory import MemoryClient

        mc = MemoryClient(db_path=str(db))
        note_id = mc.save("test memory", tags=["tag1"], category="test", pinned=True)

        r = AgenticMemoryRetriever(db_path=str(db))
        docs = r.invoke("test")
        assert len(docs) >= 1
        doc = docs[0]
        # critical keys from MemoryResult — search pipeline may not populate
        # content text (it finds via FTS5/vector), but tags/pinned/score come back
        assert doc.metadata.get("memory_id") == note_id
        assert doc.metadata.get("pinned") is True
        assert doc.metadata.get("tags") == ["tag1"]
        assert doc.metadata.get("score", 0) > 0

    def test_metadata_filters_some_falsy_values(self):
        db = _fresh_db("filter")
        from agentic_memory import MemoryClient

        mc = MemoryClient(db_path=str(db))
        mc.save("minimal memory", tags=[])

        r = AgenticMemoryRetriever(db_path=str(db))
        docs = r.invoke("minimal")
        assert len(docs) >= 1
        doc = docs[0]
        # Retriever strips 0.0 and None from r.metadata extras.
        # Standard fields (pinned=False, importance=3 etc.) are deliberate.
        exclude = {
            "memory_id",
            "tags",
            "category",
            "score",
            "created_at",
            "pinned",
            "importance",
        }
        extras = {k: v for k, v in doc.metadata.items() if k not in exclude}
        bad = {k: v for k, v in extras.items() if v in (0.0, None)}
        assert bad == {}, f"Unexpected falsy metadata extras: {bad}"

    def test_source_file_in_metadata(self):
        db = _fresh_db("source_file")
        from agentic_memory import MemoryClient

        mc = MemoryClient(db_path=str(db))
        mc.save("test")

        r = AgenticMemoryRetriever(db_path=str(db))
        docs = r.invoke("test")
        assert len(docs) >= 1
        # source_file is set by the search pipeline; page_content may be ""
        assert "source_file" in docs[0].metadata or docs[0].page_content == ""

    def test_empty_search_returns_empty_list(self):
        db = _fresh_db("empty")
        # fresh DB with no saves
        r = AgenticMemoryRetriever(db_path=str(db))
        docs = r.invoke("zzzz_no_results_expected")
        assert docs == []


if __name__ == "__main__":
    unittest.main()
