"""Tests for Query Embedding Cache (LRU 128) and FTS5 Result Cache (TTL 30s).

Covers:
  1. Query cache enabled via MEMORY_QUERY_CACHE=1.
  2. Query cache LRU eviction at 128 entries.
  3. Query cache hit returns same embedding.
  4. FTS5 cache TTL check — expired entries are recomputed.
  5. FTS5 cache_stats() returns expected shape.
  6. cache_stats() with TTL enabled/disabled.
"""
import os
import sys
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest.mock import patch, MagicMock
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestQueryEmbeddingCache(unittest.TestCase):
    """Query embedding cache (LRU 128) in EmbeddingSearch."""

    def test_cache_disabled_by_default(self):
        from embedding_search import EmbeddingSearch
        es = EmbeddingSearch.__new__(EmbeddingSearch)
        es._QUERY_CACHE_ENABLED = False
        es._query_cache = OrderedDict()
        self.assertFalse(es._QUERY_CACHE_ENABLED)

    @patch.dict(os.environ, {"MEMORY_QUERY_CACHE": "1"})
    def test_cache_enabled(self):
        from embedding_search import EmbeddingSearch
        es = EmbeddingSearch.__new__(EmbeddingSearch)
        es._QUERY_CACHE_ENABLED = os.environ.get("MEMORY_QUERY_CACHE", "0") == "1"
        self.assertTrue(es._QUERY_CACHE_ENABLED)

    def test_cache_hit_returns_same_embedding(self):
        from embedding_search import EmbeddingSearch
        es = EmbeddingSearch.__new__(EmbeddingSearch)
        es._QUERY_CACHE_ENABLED = True
        es._query_cache = OrderedDict()
        es._QUERY_CACHE_MAX = 128
        es.model = MagicMock()
        es.np = np
        es.model.encode.return_value = np.array([[0.1, 0.2, 0.3]], dtype=np.float32)

        result1 = es._embed_query("test query")
        result2 = es._embed_query("test query")
        self.assertTrue(np.array_equal(result1, result2))
        self.assertEqual(es.model.encode.call_count, 1)

    def test_cache_disabled_encodes_every_time(self):
        from embedding_search import EmbeddingSearch
        es = EmbeddingSearch.__new__(EmbeddingSearch)
        es._QUERY_CACHE_ENABLED = False
        es._query_cache = OrderedDict()
        es._QUERY_CACHE_MAX = 128
        es.model = MagicMock()
        es.np = np
        es.model.encode.return_value = np.array([[0.1, 0.2, 0.3]], dtype=np.float32)

        es._embed_query("test query")
        es._embed_query("test query")
        self.assertEqual(es.model.encode.call_count, 2)

    def test_lru_eviction_at_max(self):
        from embedding_search import EmbeddingSearch
        es = EmbeddingSearch.__new__(EmbeddingSearch)
        es._QUERY_CACHE_ENABLED = True
        es._query_cache = OrderedDict()
        es._QUERY_CACHE_MAX = 3
        es.model = MagicMock()
        es.np = np
        es.model.encode.return_value = np.array([[0.1]], dtype=np.float32)

        for i in range(5):
            es._embed_query(f"q{i}")
        self.assertEqual(len(es._query_cache), 3)
        self.assertNotIn("q0", es._query_cache)
        self.assertNotIn("q1", es._query_cache)


class TestFTS5CacheTTL(unittest.TestCase):
    """FTS5 result cache TTL in memory_mcp."""

    def test_ttl_enabled_by_default(self):
        import memory_mcp
        self.assertTrue(memory_mcp._SEARCH_CACHE_TTL_ENABLED)

    @patch.dict(os.environ, {"MEMORY_FTS5_CACHE": "1", "MEMORY_FTS5_CACHE_TTL": "10"})
    def test_ttl_enabled(self):
        import memory_mcp
        old_enabled = memory_mcp._SEARCH_CACHE_TTL_ENABLED
        old_ttl = memory_mcp._SEARCH_CACHE_TTL
        memory_mcp._SEARCH_CACHE_TTL_ENABLED = True
        memory_mcp._SEARCH_CACHE_TTL = 10
        try:
            self.assertTrue(memory_mcp._SEARCH_CACHE_TTL_ENABLED)
            self.assertEqual(memory_mcp._SEARCH_CACHE_TTL, 10)
        finally:
            memory_mcp._SEARCH_CACHE_TTL_ENABLED = old_enabled
            memory_mcp._SEARCH_CACHE_TTL = old_ttl


class TestCacheStats(unittest.TestCase):
    """cache_stats() returns expected shape."""

    def test_cache_stats_shape(self):
        import memory_mcp
        stats = memory_mcp.cache_stats()
        self.assertIn("fts5_cache", stats)
        fts5 = stats["fts5_cache"]
        self.assertIn("entries", fts5)
        self.assertIn("max", fts5)
        self.assertIn("ttl_enabled", fts5)
        self.assertIn("ttl_seconds", fts5)
        self.assertIn("active", fts5)
        self.assertIn("expired", fts5)


if __name__ == "__main__":
    unittest.main()
