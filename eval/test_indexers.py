#!/usr/bin/env python3
"""Tests for save/indexers.py — per-signal indexer wrappers.

Each wrapper function in ``save.indexers`` is a thin try/except around an
underlying indexer call (chunks, embedding, KG, facts, adaptive retention,
backlinks).  These tests verify:

1. Successful delegation to the underlying indexer.
2. Graceful exception handling (the wrapper catches and logs rather than
   letting the exception propagate).
3. Feature-flag gating (e.g. ``KG_ENABLED``, missing ``ssm_state`` column).
4. Companion writes (user_profile_access_log, CTR click proxy).

The core logic of each indexer is tested in its own module's test file:
  - ``test_auto_backlinks.py`` ← ``_index_backlinks``
  - ``test_qw5_chunk_indexing.py`` ← ``_qw5_index_chunks_for``
  - ``test_embedding_cache.py`` ← ``EmbeddingSearch.index_embedding``
  - ``test_knowledge_graph.py`` ← ``index_kg_for_memory``
  - ``test_fact_extraction.py`` ← ``index_facts_for_memory``
  - ``test_p0_p1_p2_fixes.py`` ← ``_index_adaptive_retention``

This file focuses on the **wrapper contract**.
"""

import json
import os
import pytest
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Make agentic-memory importable.
AGENTIC_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(AGENTIC_DIR))

# Make _fixtures (sibling module) importable.
EVAL_DIR = Path(__file__).resolve().parent
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

from _fixtures import bootstrap_temp_db_clean


# =============================================================================
# Helpers
# =============================================================================


def _insert_memory(
    conn: sqlite3.Connection,
    note_id: str,
    content: str = "test content",
    tags: str = "[]",
    category: str = "",
    source_file: str = "",
) -> None:
    """Populate a single memories row (all NOT-NULL columns)."""
    now = "2024-06-01T00:00:00"
    conn.execute(
        "INSERT OR IGNORE INTO memories "
        "(id, content, source_file, tags, category, created_at, updated_at, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            note_id,
            content,
            source_file or f"{note_id}.md",
            tags,
            category,
            now,
            now,
            now,
        ),
    )


def _count_rows(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# =============================================================================
# _index_backlinks wrapper
# =============================================================================


# NOTE: _index_backlinks is NOT a try/except wrapper — it is the inline
# implementation of backlink extraction.  Exceptions propagate to the
# caller (save_pipeline wraps the entire indexer sequence in a saga).
# Core logic is tested in ``test_auto_backlinks.py``.


# =============================================================================
# _index_chunks wrapper
# =============================================================================


class TestIndexChunksWrapper(unittest.TestCase):
    """Wrapper contract for _index_chunks.

    Core chunking logic is tested in ``test_qw5_chunk_indexing.py``.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="idx_chunks_")
        self.db_path = Path(self.tmp) / "memory.db"
        bootstrap_temp_db_clean(self.db_path)
        self.conn = sqlite3.connect(str(self.db_path))

    def tearDown(self):
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_delegates_to_qw5_index_chunks_for(self):
        _insert_memory(self.conn, "test/m1", "Hello. " * 200)
        self.conn.commit()
        from save.indexers import _index_chunks

        _index_chunks(self.conn, "test/m1", "Hello. " * 200)
        count = _count_rows(self.conn, "memory_chunks")
        self.assertGreater(count, 0)

    def test_single_chunk_for_short_content(self):
        _insert_memory(self.conn, "test/m1", "Short note.")
        self.conn.commit()
        from save.indexers import _index_chunks

        _index_chunks(self.conn, "test/m1", "Short note.")
        count = _count_rows(self.conn, "memory_chunks")
        self.assertEqual(count, 1)

    def test_rewrites_on_reindex(self):
        _insert_memory(self.conn, "test/m1", "Hello. " * 200)
        self.conn.commit()
        from save.indexers import _index_chunks

        _index_chunks(self.conn, "test/m1", "Hello. " * 200)
        n1 = _count_rows(self.conn, "memory_chunks")
        _index_chunks(self.conn, "test/m1", "Hello. " * 200)
        n2 = _count_rows(self.conn, "memory_chunks")
        self.assertEqual(n1, n2)

    def test_handles_missing_memory_chunks_table(self):
        self.conn.execute("DROP TABLE IF EXISTS memory_chunks")
        self.conn.commit()
        from save.indexers import _index_chunks

        _index_chunks(self.conn, "test/m1", "Hello. " * 200)
        # Should not raise

    def test_handles_none_content(self):
        _insert_memory(self.conn, "test/m1", "")
        self.conn.commit()
        from save.indexers import _index_chunks

        _index_chunks(self.conn, "test/m1", None)
        # Should not raise


# =============================================================================
# _index_embedding wrapper
# =============================================================================


class TestIndexEmbeddingWrapper(unittest.TestCase):
    """Wrapper contract for _index_embedding.

    Core embedding logic is tested in ``test_embedding_cache.py``.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="idx_emb_")
        self.db_path = Path(self.tmp) / "memory.db"
        bootstrap_temp_db_clean(self.db_path)
        self.conn = sqlite3.connect(str(self.db_path))

    def tearDown(self):
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_embedding_row(self):
        _insert_memory(self.conn, "test/m1", "Some content about embeddings.")
        self.conn.commit()
        from save.indexers import _index_embedding

        _index_embedding(
            self.conn,
            "test/m1",
            "Some content about embeddings.",
            "general",
            [],
            "test/m1.md",
        )
        self.conn.commit()
        count = _count_rows(self.conn, "memory_embeddings")
        self.assertEqual(count, 1)

    def test_overwrites_on_content_change(self):
        _insert_memory(self.conn, "test/m1", "first")
        self.conn.commit()
        from save.indexers import _index_embedding

        _index_embedding(self.conn, "test/m1", "first", "general", [], "test/m1.md")
        self.conn.commit()
        first_hash = self.conn.execute(
            "SELECT content_hash FROM memory_embeddings WHERE memory_id='test/m1'"
        ).fetchone()[0]

        _index_embedding(
            self.conn, "test/m1", "second version", "general", [], "test/m1.md"
        )
        self.conn.commit()
        second_hash = self.conn.execute(
            "SELECT content_hash FROM memory_embeddings WHERE memory_id='test/m1'"
        ).fetchone()[0]
        self.assertNotEqual(first_hash, second_hash)

    def test_handles_missing_memory_embeddings_table(self):
        self.conn.execute("DROP TABLE IF EXISTS memory_embeddings")
        self.conn.commit()
        from save.indexers import _index_embedding

        _index_embedding(self.conn, "test/m1", "content", "general", [], "test/m1.md")
        # Should not raise

    def test_handles_note_without_memories_row(self):
        from save.indexers import _index_embedding

        _index_embedding(
            self.conn, "test/no-such-note", "content", "general", [], "test.md"
        )
        # Should not raise — embedding search handles it gracefully


# =============================================================================
# _write_ssm_state
# =============================================================================





# =============================================================================
# _index_kg wrapper
# =============================================================================


class TestIndexKGWrapper(unittest.TestCase):
    """Wrapper contract for _index_kg.

    Core KG logic is tested in ``test_knowledge_graph.py``.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="idx_kg_")
        self.db_path = Path(self.tmp) / "memory.db"
        bootstrap_temp_db_clean(self.db_path)
        self.conn = sqlite3.connect(str(self.db_path))

    def tearDown(self):
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_delegates_to_kg_indexing(self):
        _insert_memory(
            self.conn, "test/m1", "John Smith works at OpenAI on the GPT model."
        )
        self.conn.commit()
        from save.indexers import _index_kg

        _index_kg(self.conn, "test/m1", "John Smith works at OpenAI on the GPT model.")
        self.conn.commit()
        count = _count_rows(self.conn, "kg_entities")
        self.assertGreater(count, 0)

    def test_disabled_kg_skips_indexing(self):
        import knowledge_graph

        old = knowledge_graph.KG_ENABLED
        knowledge_graph.KG_ENABLED = False
        try:
            _insert_memory(self.conn, "test/m1", "John Smith works at OpenAI.")
            self.conn.commit()
            from save.indexers import _index_kg

            _index_kg(self.conn, "test/m1", "John Smith works at OpenAI.")
            self.conn.commit()
            count = _count_rows(self.conn, "kg_entities")
            self.assertEqual(count, 0)
        finally:
            knowledge_graph.KG_ENABLED = old

    def test_handles_empty_content(self):
        from save.indexers import _index_kg

        _index_kg(self.conn, "test/m1", "")
        self.conn.commit()
        count = _count_rows(self.conn, "kg_entities")
        self.assertEqual(count, 0)


# =============================================================================
# _index_facts wrapper
# =============================================================================


class TestIndexFactsWrapper(unittest.TestCase):
    """Wrapper contract for _index_facts.

    Core fact-extraction logic is tested in ``test_fact_extraction.py``.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="idx_facts_")
        self.db_path = Path(self.tmp) / "memory.db"
        bootstrap_temp_db_clean(self.db_path)
        self.conn = sqlite3.connect(str(self.db_path))

    def tearDown(self):
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_delegates_to_fact_indexing(self):
        _insert_memory(
            self.conn, "test/m1", "Alice is an engineer. Bob created the API."
        )
        self.conn.commit()
        from save.indexers import _index_facts

        _index_facts(self.conn, "test/m1", "Alice is an engineer. Bob created the API.")
        self.conn.commit()
        count = _count_rows(self.conn, "kg_facts")
        self.assertGreater(count, 0)

    def test_disabled_kg_skips_fact_indexing(self):
        import knowledge_graph

        old = knowledge_graph.KG_ENABLED
        knowledge_graph.KG_ENABLED = False
        try:
            _insert_memory(self.conn, "test/m1", "Alice is an engineer.")
            self.conn.commit()
            from save.indexers import _index_facts

            _index_facts(self.conn, "test/m1", "Alice is an engineer.")
            self.conn.commit()
            count = _count_rows(self.conn, "kg_facts")
            self.assertEqual(count, 0)
        finally:
            knowledge_graph.KG_ENABLED = old

    def test_handles_empty_content(self):
        from save.indexers import _index_facts

        _index_facts(self.conn, "test/m1", "")
        self.conn.commit()
        count = _count_rows(self.conn, "kg_facts")
        self.assertEqual(count, 0)


# =============================================================================
# _index_adaptive_retention wrapper
# =============================================================================


class TestIndexAdaptiveRetention(unittest.TestCase):
    """Wrapper contract for _index_adaptive_retention.

    Core retention logic is tested in ``test_p0_p1_p2_fixes.py`` and
    ``test_adaptive_retention.py``.  Here we verify the companion writes
    (user_profile_access_log, CTR click proxy).
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="idx_ar_")
        self.db_path = Path(self.tmp) / "memory.db"
        bootstrap_temp_db_clean(self.db_path)
        self.conn = sqlite3.connect(str(self.db_path))
        _insert_memory(self.conn, "test/m1", "content for adaptive retention")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_user_profile_access_log(self):
        from save.indexers import _index_adaptive_retention

        _index_adaptive_retention(self.conn, "test/m1")
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT * FROM user_profile_access_log WHERE note_id = 'test/m1'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], "save")

    def test_writes_adaptive_retention_row(self):
        from save.indexers import _index_adaptive_retention

        _index_adaptive_retention(self.conn, "test/m1")
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT * FROM user_access_log WHERE note_id = 'test/m1'"
        ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_ctr_click_proxy_noop_when_no_ctr_row(self):
        from save.indexers import _index_adaptive_retention

        _index_adaptive_retention(self.conn, "test/m1")
        self.conn.commit()
        rows = self.conn.execute("SELECT * FROM memory_ctr_feedback").fetchall()
        self.assertEqual(len(rows), 0)

    def test_ctr_click_proxy_matches_recent_returned(self):
        self.conn.execute("DROP TABLE IF EXISTS memory_ctr_feedback")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_ctr_feedback (
                id TEXT NOT NULL,
                query_id TEXT NOT NULL,
                returned_at REAL NOT NULL,
                clicked_at REAL,
                PRIMARY KEY (id, query_id)
            )
        """)
        self.conn.execute(
            "INSERT OR IGNORE INTO memory_ctr_feedback (id, query_id, returned_at) "
            "VALUES ('test/m1', 'q1', ?)",
            (time.time() - 10,),
        )
        self.conn.commit()
        from save.indexers import _index_adaptive_retention

        _index_adaptive_retention(self.conn, "test/m1")
        self.conn.commit()
        row = self.conn.execute(
            "SELECT clicked_at FROM memory_ctr_feedback WHERE id='test/m1' AND query_id='q1'"
        ).fetchone()
        self.assertIsNotNone(row[0], "CTR click should be set by proxy")

    def test_ctr_click_proxy_skips_stale_returned(self):
        self.conn.execute("DROP TABLE IF EXISTS memory_ctr_feedback")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_ctr_feedback (
                id TEXT NOT NULL,
                query_id TEXT NOT NULL,
                returned_at REAL NOT NULL,
                clicked_at REAL,
                PRIMARY KEY (id, query_id)
            )
        """)
        self.conn.execute(
            "INSERT OR IGNORE INTO memory_ctr_feedback (id, query_id, returned_at) "
            "VALUES ('test/m1', 'q1', ?)",
            (time.time() - 360000,),
        )
        self.conn.commit()
        from save.indexers import _index_adaptive_retention

        _index_adaptive_retention(self.conn, "test/m1")
        self.conn.commit()
        row = self.conn.execute(
            "SELECT clicked_at FROM memory_ctr_feedback WHERE id='test/m1' AND query_id='q1'"
        ).fetchone()
        self.assertIsNone(row[0], "Stale CTR should not be clicked")


# =============================================================================
# Integration: all indexers called in sequence (save-pipeline-style)
# =============================================================================


class TestAllIndexersIntegration(unittest.TestCase):
    """Verify all 6 indexers compose without error, as save_pipeline does."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="idx_all_")
        self.db_path = Path(self.tmp) / "memory.db"
        bootstrap_temp_db_clean(self.db_path)
        self.conn = sqlite3.connect(str(self.db_path))
        _insert_memory(
            self.conn,
            "test/m1",
            "Alice is an engineer at OpenAI working on the GPT model. "
            "She collaborates with Bob on [[project-alpha]] and [[design-doc]]. "
            "See also the meeting notes for 2026-06-09. " * 50,
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_all_indexers_run_without_error(self):
        from save.indexers import (
            _index_backlinks,
            _index_chunks,
            _index_embedding,
            _index_kg,
            _index_facts,
            _index_adaptive_retention,
        )

        content = (
            "Alice is an engineer at OpenAI working on the GPT model. "
            "She collaborates with Bob on [[project-alpha]] and [[design-doc]]. "
            "See also the meeting notes for 2026-06-09. " * 50
        )
        _insert_memory(self.conn, "test/m2", content)
        self.conn.commit()

        _index_backlinks(self.conn, "test/m2", content)
        _index_chunks(self.conn, "test/m2", content)
        _index_embedding(self.conn, "test/m2", content, "general", [], "test/m2.md")
        _index_kg(self.conn, "test/m2", content)
        _index_facts(self.conn, "test/m2", content)
        _index_adaptive_retention(self.conn, "test/m2")
        self.conn.commit()
        # No assertion — we just verify nothing raises

    def test_backlinks_created(self):
        from save.indexers import _index_backlinks, _index_chunks

        content = "See [[project-alpha]] for details."
        _insert_memory(self.conn, "test/m2", content)
        self.conn.commit()
        _index_backlinks(self.conn, "test/m2", content)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM backlinks WHERE source_id='test/m2'"
        ).fetchone()[0]
        self.assertGreater(rows, 0)


if __name__ == "__main__":
    unittest.main()
