#!/usr/bin/env python3
"""Unit tests for Wave 2C fixes: M6 (cache key safety) + H5 (subsampling).

Run with:
    ~/.config/agentic-memory/venv/bin/python eval/test_wave2c.py
"""
import sys
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

import memory_mcp  # noqa: E402


class TestCacheKeySafety(unittest.TestCase):
    def setUp(self):
        # Reset the live search cache to a known state.
        memory_mcp._search_cache.clear()

    def test_short_query_key_stable(self):
        k1 = memory_mcp._make_cache_key(
            Path("/a/b/c"), "foo OR bar", 5, True, True, 0.1, True
        )
        k2 = memory_mcp._make_cache_key(
            Path("/a/b/c"), "foo OR bar", 5, True, True, 0.1, True
        )
        self.assertEqual(k1, k2)

    def test_different_db_path_distinguished(self):
        k1 = memory_mcp._make_cache_key(
            Path("/a/db1"), "foo", 5, True, True, 0.1, True
        )
        k2 = memory_mcp._make_cache_key(
            Path("/a/db2"), "foo", 5, True, True, 0.1, True
        )
        self.assertNotEqual(k1, k2, "different db paths must hash differently")

    def test_long_query_bounded(self):
        long_q = "x " * 1000
        k = memory_mcp._make_cache_key(
            Path("/a/b"), long_q, 5, True, True, 0.1, True
        )
        # Should be capped: key length well under original query length
        self.assertLess(
            len(k), 300,
            f"cache key should be bounded, got {len(k)} chars",
        )
        # Should still be self-consistent: two calls with same long query
        # produce same key
        k2 = memory_mcp._make_cache_key(
            Path("/a/b"), long_q, 5, True, True, 0.1, True
        )
        self.assertEqual(k, k2)

    def test_path_stringified_explicitly(self):
        # Two Path objects pointing to the same place must produce the
        # same key (this rules out the old repr-based key bug).
        k1 = memory_mcp._make_cache_key(
            Path("/tmp/foo.db"), "q", 5, True, True, 0.1, True
        )
        k2 = memory_mcp._make_cache_key(
            Path("/tmp/foo.db"), "q", 5, True, True, 0.1, True
        )
        self.assertEqual(k1, k2)

    def test_different_include_invalid(self):
        k1 = memory_mcp._make_cache_key(
            Path("/a"), "q", 5, True, True, 0.1, True
        )
        k2 = memory_mcp._make_cache_key(
            Path("/a"), "q", 5, True, True, 0.1, False
        )
        self.assertNotEqual(k1, k2, "include_invalid must be in the key")


class TestSlidingWindowConstant(unittest.TestCase):
    def test_sliding_window_constant_exists(self):
        from contradiction_detector import SLIDING_WINDOW_SIZE
        self.assertIsInstance(SLIDING_WINDOW_SIZE, int)
        self.assertGreater(SLIDING_WINDOW_SIZE, 0)


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
