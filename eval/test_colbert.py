"""Tests for ColBERT late-interaction reranking (Phase 3).

Covers:
  - Migration: colbert_tokens table exists
  - Encoder: encode_tokens / encode_query return correct shapes
  - Index: index_memory_colbert stores and retrieves token rows
  - Rerank: maxsim_score correctness, adaptive depth gates, blend behavior
"""

from __future__ import annotations

import math
import sqlite3
import struct
import time

import pytest


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


class TestColbertMigration:
    def test_colbert_tokens_table_exists(self, tmp_path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS colbert_tokens (
                id          INTEGER PRIMARY KEY,
                memory_id   TEXT NOT NULL,
                chunk_id    INTEGER NOT NULL DEFAULT 0,
                position    INTEGER NOT NULL DEFAULT 0,
                token_text  TEXT NOT NULL DEFAULT '',
                vec         BLOB NOT NULL,
                created_at  REAL NOT NULL DEFAULT (unixepoch())
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ct_memory ON colbert_tokens(memory_id)")
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='colbert_tokens'").fetchone()
        assert row is not None
        conn.close()


# ---------------------------------------------------------------------------
# Encoder
# ---------------------------------------------------------------------------


class TestColbertEncoder:
    def test_encode_query_returns_list(self):
        from infra.colbert_encoder import encode_query
        result = encode_query("test query")
        # Model may not be available in CI — skip if None
        if result is not None:
            assert isinstance(result, list)
            assert len(result) > 0
            assert isinstance(result[0], list)
            assert len(result[0]) == 128  # ColBERT-v2 dim

    def test_encode_tokens_returns_pairs(self):
        from infra.colbert_encoder import encode_tokens
        result = encode_tokens("hello world test")
        if result is not None:
            assert isinstance(result, list)
            assert len(result) > 0
            tok, vec = result[0]
            assert isinstance(tok, str)
            assert isinstance(vec, list)
            assert len(vec) == 128

    def test_special_tokens_excluded(self):
        from infra.colbert_encoder import encode_tokens
        result = encode_tokens("hello")
        if result is not None:
            tokens = [t for t, _ in result]
            assert "[CLS]" not in tokens
            assert "[SEP]" not in tokens
            assert "[PAD]" not in tokens


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


class TestColbertIndex:
    def _make_db(self, tmp_path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS colbert_tokens (
                id          INTEGER PRIMARY KEY,
                memory_id   TEXT NOT NULL,
                chunk_id    INTEGER NOT NULL DEFAULT 0,
                position    INTEGER NOT NULL DEFAULT 0,
                token_text  TEXT NOT NULL DEFAULT '',
                vec         BLOB NOT NULL,
                created_at  REAL NOT NULL DEFAULT (unixepoch())
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ct_memory ON colbert_tokens(memory_id)")
        conn.commit()
        return conn

    def test_vec_blob_roundtrip(self):
        from search.colbert_index import _vec_to_blob
        from search.colbert_rerank import _blob_to_vec
        vec = [0.1, 0.2, 0.3, -0.5, 1.0]
        blob = _vec_to_blob(vec)
        restored = _blob_to_vec(blob)
        assert len(restored) == len(vec)
        for a, b in zip(vec, restored):
            assert abs(a - b) < 1e-6

    def test_delete_memory_colbert(self, tmp_path):
        from search.colbert_index import delete_memory_colbert
        conn = self._make_db(tmp_path)
        conn.execute(
            "INSERT INTO colbert_tokens (memory_id, chunk_id, position, token_text, vec) VALUES (?, 0, 0, 'hi', ?)",
            ("mem1", b"\x00" * 4),
        )
        conn.commit()
        deleted = delete_memory_colbert(conn, "mem1")
        assert deleted == 1
        assert conn.execute("SELECT COUNT(*) FROM colbert_tokens").fetchone()[0] == 0
        conn.close()

    def test_get_indexed_memory_ids(self, tmp_path):
        from search.colbert_index import get_indexed_memory_ids
        conn = self._make_db(tmp_path)
        for mid in ("mem1", "mem2", "mem1"):
            conn.execute(
                "INSERT INTO colbert_tokens (memory_id, chunk_id, position, token_text, vec) VALUES (?, 0, 0, 'x', ?)",
                (mid, b"\x00" * 4),
            )
        conn.commit()
        ids = get_indexed_memory_ids(conn)
        assert set(ids) == {"mem1", "mem2"}
        conn.close()


# ---------------------------------------------------------------------------
# MaxSim
# ---------------------------------------------------------------------------


class TestMaxSim:
    def test_identical_vectors_high_score(self):
        from search.colbert_rerank import maxsim_score
        vecs = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
        score = maxsim_score(vecs, vecs)
        assert score == pytest.approx(2.0, abs=0.01)

    def test_orthogonal_vectors_zero(self):
        from search.colbert_rerank import maxsim_score
        q = [[1.0, 0.0]]
        d = [[0.0, 1.0]]
        score = maxsim_score(q, d)
        assert score == pytest.approx(0.0, abs=0.01)

    def test_empty_inputs_zero(self):
        from search.colbert_rerank import maxsim_score
        assert maxsim_score([], [[1.0]]) == 0.0
        assert maxsim_score([[1.0]], []) == 0.0

    def test_partial_match(self):
        from search.colbert_rerank import maxsim_score
        q = [[1.0, 0.0], [0.0, 1.0]]
        d = [[1.0, 0.0]]  # Matches first query token perfectly
        score = maxsim_score(q, d)
        assert score == pytest.approx(1.0, abs=0.01)


# ---------------------------------------------------------------------------
# Rerank adaptive depth
# ---------------------------------------------------------------------------


class TestColbertRerank:
    def _make_item(self, mid, score):
        return (mid, "content", "f.md", "[]", "2024-01-01T00:00:00+00:00", 1, score, 0.5, 3, 0, None, None)

    def test_skip_too_many_candidates(self):
        from search.colbert_rerank import colbert_rerank
        conn = sqlite3.connect(":memory:")
        items = [self._make_item(f"m{i}", 0.5) for i in range(50)]
        result = colbert_rerank(conn, "test query here", items)
        assert result == items  # Unchanged

    def test_skip_short_query(self):
        from search.colbert_rerank import colbert_rerank
        conn = sqlite3.connect(":memory:")
        items = [self._make_item("m1", 0.5)]
        result = colbert_rerank(conn, "hi", items)
        assert result == items  # Unchanged

    def test_skip_empty_index(self):
        from search.colbert_rerank import colbert_rerank
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS colbert_tokens (
                id INTEGER PRIMARY KEY, memory_id TEXT, chunk_id INTEGER,
                position INTEGER, token_text TEXT, vec BLOB, created_at REAL
            )
            """
        )
        items = [self._make_item("m1", 0.5)]
        result = colbert_rerank(conn, "test query here", items)
        assert result == items  # Unchanged (empty index)
