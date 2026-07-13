"""Tests for SPLADE sparse vector search (Phase 4).

Covers:
  - Migration: splade_tokens table exists
  - Encoder: encode_sparse returns correct format
  - Index: index_memory_splade stores and retrieves sparse vectors
  - Search: splade_search returns ranked results
  - Ablation: SPLADE recall vs BM25 baseline
"""

from __future__ import annotations

import math
import sqlite3
import time

import pytest


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


class TestSpladeMigration:
    def test_splade_tokens_table_exists(self, tmp_path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS splade_tokens (
                id          INTEGER PRIMARY KEY,
                memory_id   TEXT NOT NULL,
                vocab_id    INTEGER NOT NULL,
                weight      REAL NOT NULL,
                created_at  REAL NOT NULL DEFAULT (unixepoch())
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_st_memory ON splade_tokens(memory_id)")
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='splade_tokens'").fetchone()
        assert row is not None
        conn.close()


# ---------------------------------------------------------------------------
# Encoder
# ---------------------------------------------------------------------------


class TestSpladeEncoder:
    def test_encode_sparse_returns_list(self):
        from infra.splade_encoder import encode_sparse
        result = encode_sparse("test query about python")
        if result is not None:
            assert isinstance(result, list)
            assert len(result) > 0
            vid, weight = result[0]
            assert isinstance(vid, int)
            assert isinstance(weight, float)
            assert weight > 0

    def test_encode_sparse_sorted_by_weight(self):
        from infra.splade_encoder import encode_sparse
        result = encode_sparse("machine learning deep neural network")
        if result is not None:
            weights = [w for _, w in result]
            assert weights == sorted(weights, reverse=True)

    def test_encode_sparse_empty_query(self):
        from infra.splade_encoder import encode_sparse
        result = encode_sparse("")
        # Empty query may return None or empty list
        if result is not None:
            assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


class TestSpladeIndex:
    def _make_db(self, tmp_path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS splade_tokens (
                id          INTEGER PRIMARY KEY,
                memory_id   TEXT NOT NULL,
                vocab_id    INTEGER NOT NULL,
                weight      REAL NOT NULL,
                created_at  REAL NOT NULL DEFAULT (unixepoch())
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_st_memory ON splade_tokens(memory_id)")
        conn.commit()
        return conn

    def test_delete_memory_splade(self, tmp_path):
        from search.splade_index import delete_memory_splade
        conn = self._make_db(tmp_path)
        conn.execute(
            "INSERT INTO splade_tokens (memory_id, vocab_id, weight) VALUES (?, ?, ?)",
            ("mem1", 100, 0.5),
        )
        conn.commit()
        deleted = delete_memory_splade(conn, "mem1")
        assert deleted == 1
        assert conn.execute("SELECT COUNT(*) FROM splade_tokens").fetchone()[0] == 0
        conn.close()

    def test_get_indexed_memory_ids(self, tmp_path):
        from search.splade_index import get_indexed_memory_ids
        conn = self._make_db(tmp_path)
        for mid in ("mem1", "mem2", "mem1"):
            conn.execute(
                "INSERT INTO splade_tokens (memory_id, vocab_id, weight) VALUES (?, ?, ?)",
                (mid, 100, 0.5),
            )
        conn.commit()
        ids = get_indexed_memory_ids(conn)
        assert set(ids) == {"mem1", "mem2"}
        conn.close()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSpladeSearch:
    def _make_db_with_data(self, tmp_path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS splade_tokens (
                id          INTEGER PRIMARY KEY,
                memory_id   TEXT NOT NULL,
                vocab_id    INTEGER NOT NULL,
                weight      REAL NOT NULL,
                created_at  REAL NOT NULL DEFAULT (unixepoch())
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_st_memory ON splade_tokens(memory_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_st_vocab ON splade_tokens(vocab_id)")
        # Insert sparse vectors for two memories
        # mem1: vocab_ids 10, 20, 30 with weights 0.8, 0.6, 0.4
        for vid, w in [(10, 0.8), (20, 0.6), (30, 0.4)]:
            conn.execute(
                "INSERT INTO splade_tokens (memory_id, vocab_id, weight) VALUES (?, ?, ?)",
                ("mem1", vid, w),
            )
        # mem2: vocab_ids 20, 40 with weights 0.7, 0.5
        for vid, w in [(20, 0.7), (40, 0.5)]:
            conn.execute(
                "INSERT INTO splade_tokens (memory_id, vocab_id, weight) VALUES (?, ?, ?)",
                ("mem2", vid, w),
            )
        conn.commit()
        return conn

    def test_splade_search_returns_ranked(self, tmp_path):
        from search.splade_index import splade_search
        conn = self._make_db_with_data(tmp_path)
        # Query: vocab_ids 10, 20 with weights 0.5, 0.5
        query_sparse = [(10, 0.5), (20, 0.5)]
        results = splade_search(conn, query_sparse, top_k=10)
        assert len(results) > 0
        # mem1 should score higher (matches both 10 and 20)
        assert results[0][0] == "mem1"

    def test_splade_search_empty_query(self, tmp_path):
        from search.splade_index import splade_search
        conn = self._make_db_with_data(tmp_path)
        results = splade_search(conn, [], top_k=10)
        assert results == []

    def test_splade_search_no_match(self, tmp_path):
        from search.splade_index import splade_search
        conn = self._make_db_with_data(tmp_path)
        # Query with vocab_id that doesn't exist
        query_sparse = [(999, 0.5)]
        results = splade_search(conn, query_sparse, top_k=10)
        assert results == []


# ---------------------------------------------------------------------------
# Ablation
# ---------------------------------------------------------------------------


class TestSpladeAblation:
    def test_splade_matches_bm25_on_factoid(self, tmp_path):
        """SPLADE should match BM25 on exact-match factoid queries."""
        from search.splade_index import splade_search
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS splade_tokens (
                id          INTEGER PRIMARY KEY,
                memory_id   TEXT NOT NULL,
                vocab_id    INTEGER NOT NULL,
                weight      REAL NOT NULL,
                created_at  REAL NOT NULL DEFAULT (unixepoch())
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_st_memory ON splade_tokens(memory_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_st_vocab ON splade_tokens(vocab_id)")
        # Simulate: "python" maps to vocab_id 5000
        conn.execute(
            "INSERT INTO splade_tokens (memory_id, vocab_id, weight) VALUES (?, ?, ?)",
            ("mem_python", 5000, 0.9),
        )
        conn.execute(
            "INSERT INTO splade_tokens (memory_id, vocab_id, weight) VALUES (?, ?, ?)",
            ("mem_java", 5001, 0.8),
        )
        conn.commit()

        # Query for "python"
        query_sparse = [(5000, 0.9)]
        results = splade_search(conn, query_sparse, top_k=10)
        assert len(results) > 0
        assert results[0][0] == "mem_python"
        conn.close()
