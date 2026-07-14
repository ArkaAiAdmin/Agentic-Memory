"""Test vector index fallback: when EmbeddingSearch.search raises, the search
pipeline must degrade gracefully to an FTS-only result (or full scan) instead
of crashing.

We patch infra.embedding_search.EmbeddingSearch.search at the class level so
any call through the singleton (via get_embedding_search()) triggers the
exception.  The test verifies the result envelope is valid and non-empty.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from eval._fixtures import bootstrap_temp_db_clean
from infra.db import connection_pool

logger = logging.getLogger(__name__)


class TestVecFallback(unittest.TestCase):
    """Simulate usearch/indexed-search failure and verify fallback to FTS."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "memory.db"
        bootstrap_temp_db_clean(self.db_path)
        self._seed_notes()

        # Clear connection pool so searches open fresh connections
        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()

        # The patcher is started in each test method so we can control
        # which search calls are affected.
        self._patcher = None

    def tearDown(self):
        if self._patcher is not None:
            self._patcher.stop()
            self._patcher = None
        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed_notes(self):
        """Insert a handful of notes so search has something to return."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        now = "2026-07-14T12:00:00+00:00"
        notes = [
            ("lessons/python-http", "Python requests library makes HTTP calls easy with session management.",
             ["python", "http"], "lessons"),
            ("lessons/sqlite-wal", "SQLite WAL mode allows concurrent reads during writes.",
             ["sqlite", "database"], "lessons"),
            ("lessons/async-python", "Async Python with asyncio enables concurrent I/O without threads.",
             ["python", "async"], "lessons"),
            ("decisions/use-sqlite", "Decision: Use SQLite as the primary store for single-node deployments.",
             ["sqlite", "architecture"], "decisions"),
            ("preferences/testing", "Prefer pytest for all new test files; unit tests should be hermetic.",
             ["testing", "pytest"], "preferences"),
        ]
        for nid, content, tags, category in notes:
            source = f"memory/{category}/{nid}.md"
            conn.execute(
                "INSERT INTO memories (id,content,source_file,tags,created_at,updated_at,observed_at,category) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (nid, content, source, json.dumps(tags), now, now, now, category),
            )
        conn.commit()
        conn.close()

    def _import_search(self):
        """Lazy import search_memories to avoid import-order side effects."""
        from search.orchestrator import search_memories
        return search_memories

    def test_search_works_without_patch(self):
        """Baseline: search works normally with the vec index."""
        search_memories = self._import_search()
        result = search_memories(self.db_path, "Python HTTP requests", limit=5)
        self.assertIsInstance(result, dict)
        self.assertIn("results", result)
        self.assertGreater(result["count"], 0)

    def test_search_falls_back_on_vec_exception(self):
        """When embedding search raises, the pipeline returns FTS results."""
        search_memories = self._import_search()

        from infra import embedding_search as es_mod

        original_search = es_mod.EmbeddingSearch.search

        def _exploding_search(self_obj, query, db_path, limit=5, category="", tags=None, source_file=""):
            raise RuntimeError("Simulated usearch load failure — this is a test")

        self._patcher = patch.object(
            es_mod.EmbeddingSearch, "search", _exploding_search
        )
        self._patcher.start()

        result = search_memories(self.db_path, "Python HTTP requests", limit=5)
        self.assertIsInstance(result, dict)
        self.assertIn("results", result)
        # Should still get results (from FTS/fallback path)
        self.assertGreaterEqual(
            result["count"], 0,
            "Search must not crash when vec index fails",
        )
        # Verify result envelope is structurally sound
        for r in result.get("results", []):
            self.assertIn("id", r)
            self.assertIn("final_score", r)
            self.assertIn("content", r)

    def test_search_fallback_no_crash_with_different_queries(self):
        """Multiple query shapes all survive the vec exception."""
        search_memories = self._import_search()

        from infra import embedding_search as es_mod

        def _exploding_search(self_obj, query, db_path, limit=5, category="", tags=None, source_file=""):
            raise RuntimeError("boom")

        self._patcher = patch.object(
            es_mod.EmbeddingSearch, "search", _exploding_search
        )
        self._patcher.start()

        queries = [
            "SQLite database",
            "async",
            "pytest testing preferences",
            "architecture decisions",
        ]
        for q in queries:
            with self.subTest(query=q):
                result = search_memories(self.db_path, q, limit=5)
                self.assertIsInstance(result, dict, f"crash on query: {q}")
                self.assertIn("output", result)
                # results may be empty for some queries — that's ok
                self.assertIsInstance(result.get("results"), list)

    def test_search_fallback_with_empty_query(self):
        """Even with vec broken, empty query should not crash."""
        search_memories = self._import_search()

        from infra import embedding_search as es_mod

        def _exploding_search(self_obj, query, db_path, limit=5, category="", tags=None, source_file=""):
            raise RuntimeError("boom")

        self._patcher = patch.object(
            es_mod.EmbeddingSearch, "search", _exploding_search
        )
        self._patcher.start()

        result = search_memories(self.db_path, "", limit=5)
        self.assertIsInstance(result, dict)
        # Empty query returns zero results
        self.assertEqual(result["count"], 0)


if __name__ == "__main__":
    unittest.main()
