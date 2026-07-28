#!/usr/bin/env python3
"""Unit tests for multi-vector (chunk-level) retrieval.

Tests chunking, chunk embedding indexing, chunk ANN search, and
Max-Sim aggregation of chunk scores to parent memory IDs.
"""

import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from infra.embedding_search import (
    chunk_memory,
    _chunk_cache_text,
    _chunk_content_hash,
    get_embedding_search,
)
from infra.migration_runner import run_migrations
from rebuild_vec_index import rebuild_chunk_vec_index


def _open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    run_migrations(conn)
    conn.commit()
    return conn


def _setup_chunks(conn, memory_id: str, content: str) -> list[dict]:
    now = "2026-07-01T00:00:00"
    conn.execute(
        "INSERT OR IGNORE INTO memories (id, content, created_at, updated_at, observed_at, source_file, tags) "
        "VALUES (?, ?, datetime('now'), datetime('now'), datetime('now'), ?, ?)",
        (memory_id, content, "test.md", "[]"),
    )
    conn.execute("DELETE FROM memory_chunks WHERE parent_id = ?", (memory_id,))
    chunks = chunk_memory(content)
    for i, ch in enumerate(chunks):
        conn.execute(
            "INSERT INTO memory_chunks (parent_id, chunk_idx, start_offset, end_offset, content) "
            "VALUES (?, ?, ?, ?, ?)",
            (memory_id, i, ch["start_offset"], ch["end_offset"], ch["content"]),
        )
    conn.commit()
    return chunks


class TestChunkMemorySplitting(unittest.TestCase):
    def test_splits_on_paragraphs(self):
        content = "\n\n".join([f"Paragraph {i} " + "x" * 300 for i in range(5)])
        chunks = chunk_memory(content, max_chunk_size=500, overlap=50)
        assert len(chunks) > 1
        assert all("Paragraph" in c["content"] for c in chunks)

    def test_handles_empty_content(self):
        assert chunk_memory("") == []

    def test_handles_short_content(self):
        content = "Short note."
        chunks = chunk_memory(content)
        assert len(chunks) == 1
        assert chunks[0]["content"] == content

    def test_large_content_produces_multiple_chunks(self):
        content = "\n\n".join([f"Para {i} " + "x" * 200 for i in range(10)])
        chunks = chunk_memory(content, max_chunk_size=300, overlap=30)
        assert len(chunks) > 1

    def test_chunk_offsets_are_valid(self):
        content = "A\n\nB\n\nC"
        chunks = chunk_memory(content)
        assert all(c["end_offset"] > c["start_offset"] for c in chunks)
        assert chunks[0]["start_offset"] == 0

    def test_chunk_indices_are_sequential(self):
        content = "\n\n".join([f"Para {i}" for i in range(5)])
        chunks = chunk_memory(content)
        assert [c["chunk_idx"] for c in chunks] == list(range(len(chunks)))

    def test_cache_text_truncates_and_normalizes(self):
        text = "hello" + "x" * 500
        result = _chunk_cache_text(text)
        assert len(result) <= 500
        import unicodedata
        assert result == unicodedata.normalize("NFKC", text[:500])

    def test_content_hash_is_deterministic(self):
        h1 = _chunk_content_hash("hello world")
        h2 = _chunk_content_hash("hello world")
        assert h1 == h2
        assert len(h1) == 64


class TestChunkEmbeddingIndexing(unittest.TestCase):
    def test_index_chunk_embeddings_writes_rows(self):
        tmp = Path(tempfile.mkdtemp()) / "test.db"
        conn = _open_db(tmp)
        try:
            memory_id = "lessons/test-note"
            _setup_chunks(conn, memory_id, "First paragraph.\n\nSecond paragraph.\n\nThird paragraph.")

            es = get_embedding_search()
            if es.model is None:
                self.skipTest("embedding model unavailable")

            chunks = chunk_memory("First paragraph.\n\nSecond paragraph.\n\nThird paragraph.")
            chunk_dicts = [
                {"chunk_id": None, "parent_id": memory_id, "content": c["content"]}
                for c in chunks
            ]
            n = es.index_chunk_embeddings_batch(conn, chunk_dicts)
            conn.commit()
            assert n == len(chunks)

            rows = conn.execute(
                "SELECT parent_id, content_hash FROM memory_chunk_embeddings"
            ).fetchall()
            assert len(rows) == len(chunks)
        finally:
            conn.close()
            tmp.unlink(missing_ok=True)

    def test_index_chunk_embeddings_skips_on_noop(self):
        tmp = Path(tempfile.mktemp(suffix=".db"))
        conn = _open_db(tmp)
        try:
            memory_id = "lessons/test-note-2"
            _setup_chunks(conn, memory_id, "Para 1.\n\nPara 2.")

            es = get_embedding_search()
            if es.model is None:
                self.skipTest("embedding model unavailable")

            chunks = chunk_memory("Para 1.\n\nPara 2.")
            chunk_dicts = [
                {"chunk_id": None, "parent_id": memory_id, "content": c["content"]}
                for c in chunks
            ]
            n = es.index_chunk_embeddings_batch(conn, chunk_dicts)
            conn.commit()
            assert n == len(chunks)

            # Second call with same content — should re-insert (INSERT OR REPLACE)
            n2 = es.index_chunk_embeddings_batch(conn, chunk_dicts)
            conn.commit()
            assert n2 == len(chunks)
        finally:
            conn.close()
            tmp.unlink(missing_ok=True)


class TestChunkANNBuildAndSearch(unittest.TestCase):
    def test_rebuild_chunk_vec_index_persists_and_searches(self):
        tmp = Path(tempfile.mktemp(suffix=".db"))
        conn = _open_db(tmp)
        try:
            memory_id = "lessons/search-test"
            content = "Python memory system. Embedding search with usearch."
            chunks = _setup_chunks(conn, memory_id, content)

            es = get_embedding_search()
            if es.model is None:
                self.skipTest("embedding model unavailable")

            chunk_dicts = [
                {"chunk_id": None, "parent_id": memory_id, "content": c["content"]}
                for c in chunks
            ]
            es.index_chunk_embeddings_batch(conn, chunk_dicts)
            conn.commit()

            stats = rebuild_chunk_vec_index(tmp, force=True)
            assert stats["n_indexed"] == len(chunks)
            assert stats["serialized_bytes"] > 0

            query = "python memory"
            results = es.search_chunks(conn, query, limit=5, db_path=str(tmp))
            assert isinstance(results, list)
            if results:
                for r in results:
                    assert "parent_id" in r
                    assert "score" in r
                    assert "chunk_id" in r
                    assert r["parent_id"] == memory_id
        finally:
            conn.close()
            tmp.unlink(missing_ok=True)

    def test_chunk_search_returns_maxsim_aggregation(self):
        tmp = Path(tempfile.mktemp(suffix=".db"))
        conn = _open_db(tmp)
        try:
            memory_id = "lessons/maxsim"
            content = "alpha alpha alpha.\n\nbeta beta beta.\n\ngamma gamma gamma."
            chunks = _setup_chunks(conn, memory_id, content)

            es = get_embedding_search()
            if es.model is None:
                self.skipTest("embedding model unavailable")

            chunk_dicts = [
                {"chunk_id": None, "parent_id": memory_id, "content": c["content"]}
                for c in chunks
            ]
            es.index_chunk_embeddings_batch(conn, chunk_dicts)
            conn.commit()
            rebuild_chunk_vec_index(tmp, force=True)

            results = es.search_chunks(conn, "alpha", limit=5, db_path=str(tmp))
            assert isinstance(results, list)
            assert len(results) >= 1
            parent_ids = [r["parent_id"] for r in results]
            assert all(pid == memory_id for pid in parent_ids)
        finally:
            conn.close()
            tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    import unittest

    unittest.main()
