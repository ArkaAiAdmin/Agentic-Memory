"""Tests for answer-level reranking (Phase 5).

Covers:
  - Snippet extraction: best window selection
  - Scoring: CE and keyword fallback
  - Rerank: blend behavior, cache, pre-computation
"""

from __future__ import annotations

import sqlite3
import time

import pytest


# ---------------------------------------------------------------------------
# Snippet extraction
# ---------------------------------------------------------------------------


class TestSnippetExtraction:
    def test_short_content_returned_as_is(self):
        from search.answer_rerank import _extract_snippet
        content = "short content"
        snippet = _extract_snippet(content, "query", max_tokens=100)
        assert snippet == content

    def test_empty_content_returns_empty(self):
        from search.answer_rerank import _extract_snippet
        snippet = _extract_snippet("", "query")
        assert snippet == ""

    def test_snippet_contains_query_words(self):
        from search.answer_rerank import _extract_snippet
        # Create content with a specific keyword in the middle
        words = ["word"] * 50 + ["important", "keyword", "here"] + ["word"] * 50
        content = " ".join(words)
        snippet = _extract_snippet(content, "important keyword", max_tokens=10)
        assert "important" in snippet.lower() or "keyword" in snippet.lower()

    def test_snippet_max_tokens_respected(self):
        from search.answer_rerank import _extract_snippet
        content = " ".join(["word"] * 500)
        snippet = _extract_snippet(content, "query", max_tokens=20)
        assert len(snippet.split()) <= 30  # Allow some expansion


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


class TestScoring:
    def test_empty_inputs_zero(self):
        from search.answer_rerank import _score_snippet
        assert _score_snippet("", "snippet") == 0.0
        assert _score_snippet("query", "") == 0.0

    def test_keyword_fallback(self):
        from search.answer_rerank import _score_snippet
        score = _score_snippet("python testing", "python is great for testing")
        assert score > 0.0

    def test_no_match_zero(self):
        from search.answer_rerank import _score_snippet
        score = _score_snippet("java kotlin", "python testing rust")
        assert score == 0.0


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class TestAnswerRerankCache:
    def _make_db(self, tmp_path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS answer_rerank_cache (
                memory_id   TEXT NOT NULL,
                query_hash  TEXT NOT NULL,
                score       REAL NOT NULL,
                snippet     TEXT NOT NULL,
                created_at  REAL NOT NULL DEFAULT (unixepoch()),
                UNIQUE (memory_id, query_hash)
            )
            """
        )
        conn.commit()
        return conn

    def test_cache_hit(self, tmp_path):
        from search.answer_rerank import answer_rerank, _ensure_cache_schema
        conn = self._make_db(tmp_path)
        _ensure_cache_schema(conn)

        # Pre-populate cache
        conn.execute(
            "INSERT INTO answer_rerank_cache (memory_id, query_hash, score, snippet, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("mem1", str(hash("test query")), 0.9, "cached snippet", time.time()),
        )
        conn.commit()

        # Create a candidate item (12-tuple)
        item = ("mem1", "content here", "f.md", "[]", "2024-01-01T00:00:00+00:00", 1, 0.5, 0.5, 3, 0, None, None)
        result = answer_rerank(conn, "test query", [item])

        # Should use cached score (0.9) blended with original (0.5)
        assert len(result) == 1
        score = result[0][6]
        assert score > 0.5  # Blend should increase score

    def test_clear_stale_cache(self, tmp_path):
        from search.answer_rerank import clear_stale_cache, _ensure_cache_schema
        conn = self._make_db(tmp_path)
        _ensure_cache_schema(conn)

        # Insert old entry
        old_time = time.time() - (8 * 86400)  # 8 days ago
        conn.execute(
            "INSERT INTO answer_rerank_cache (memory_id, query_hash, score, snippet, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("mem1", "hash1", 0.5, "snippet", old_time),
        )
        # Insert recent entry
        conn.execute(
            "INSERT INTO answer_rerank_cache (memory_id, query_hash, score, snippet, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("mem2", "hash2", 0.7, "snippet", time.time()),
        )
        conn.commit()

        cleared = clear_stale_cache(conn, max_age_days=7)
        assert cleared == 1
        remaining = conn.execute("SELECT COUNT(*) FROM answer_rerank_cache").fetchone()[0]
        assert remaining == 1
        conn.close()


# ---------------------------------------------------------------------------
# Pre-computation
# ---------------------------------------------------------------------------


class TestPrecompute:
    def test_precompute_for_memory(self, tmp_path):
        from search.answer_rerank import precompute_for_memory, _ensure_cache_schema
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        _ensure_cache_schema(conn)

        content = "Python is a great language for machine learning and data science."
        queries = ["python programming", "machine learning", "unrelated query"]
        scored = precompute_for_memory(conn, "mem1", content, queries)

        assert scored == 3
        rows = conn.execute("SELECT COUNT(*) FROM answer_rerank_cache").fetchone()[0]
        assert rows == 3
        conn.close()

    def test_precompute_empty_content(self, tmp_path):
        from search.answer_rerank import precompute_for_memory, _ensure_cache_schema
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        _ensure_cache_schema(conn)

        scored = precompute_for_memory(conn, "mem1", "", ["query"])
        assert scored == 0
        conn.close()


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


class TestAnswerRerankIntegration:
    def test_rerank_blends_scores(self, tmp_path):
        from search.answer_rerank import answer_rerank, _ensure_cache_schema
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        _ensure_cache_schema(conn)

        # Candidate with low original score but highly relevant content
        item = (
            "mem1",
            "Python testing is important for software quality.",
            "f.md", "[]", "2024-01-01T00:00:00+00:00",
            1, 0.3,  # low original score
            0.5, 3, 0, None, None,
        )
        result = answer_rerank(conn, "python testing", [item], blend=0.5)
        assert len(result) == 1
        # Answer score should be high (content matches query well)
        # Blended score should be between original and answer score
        assert result[0][6] > 0.3
        conn.close()

    def test_rerank_preserves_order_contract(self, tmp_path):
        from search.answer_rerank import answer_rerank, _ensure_cache_schema
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        _ensure_cache_schema(conn)

        # Two items: one relevant, one not
        item1 = ("mem1", "Python testing framework", "f.md", "[]", "2024-01-01T00:00:00+00:00", 1, 0.5, 0.5, 3, 0, None, None)
        item2 = ("mem2", "Java enterprise application", "f.md", "[]", "2024-01-01T00:00:00+00:00", 2, 0.4, 0.5, 3, 0, None, None)
        result = answer_rerank(conn, "python testing", [item1, item2])

        # mem1 should rank higher (relevant content)
        assert result[0][0] == "mem1"
        conn.close()
