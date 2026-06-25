#!/usr/bin/env python3
"""Unit tests for embedding_search.py.

Targets the pure, model-independent surface:
  * Vector-cache primitives (``_vec_cache_get``/``_vec_cache_put``,
    ``clear_vec_cache``)
  * Content hashing + NFKC normalization (``_cache_text``)
  * Tag JSON parsing (``_parse_tags``)
  * Context-prefix construction (``_build_context_prefix``)
  * Pinned model identity constants (``MODEL_ID``, ``MODEL_REVISION``,
    ``UNINDEXED_SAFETY_NET_LIMIT``)

We deliberately avoid instantiating ``EmbeddingSearch`` because that
loads the model2vec model and snapshots a 256 MB file from
HuggingFace; that's a heavy integration test, not a unit test. The
class is exercised end-to-end by ``test_vec_index_search.py`` already.
"""

import os
import sys
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))
sys.path.insert(0, str(INSTALL_DIR / "eval"))


class TestPinnedConstants(unittest.TestCase):
    """The pinned model identity must not drift silently."""

    def test_model_id_is_potion(self):
        from embedding_search import MODEL_ID

        self.assertEqual(MODEL_ID, "minishlab/potion-base-8M")

    def test_model_revision_is_pinned_sha(self):
        from embedding_search import MODEL_REVISION

        # 40-char git SHA. If this changes, embedding scores shift.
        self.assertEqual(len(MODEL_REVISION), 40)
        self.assertTrue(all(c in "0123456789abcdef" for c in MODEL_REVISION))

    def test_unindexed_safety_net_limit_is_positive(self):
        from embedding_search import UNINDEXED_SAFETY_NET_LIMIT

        self.assertIsInstance(UNINDEXED_SAFETY_NET_LIMIT, int)
        self.assertGreater(UNINDEXED_SAFETY_NET_LIMIT, 0)
        # Sanity: should be a small O(1k) cap, not unbounded.
        self.assertLessEqual(UNINDEXED_SAFETY_NET_LIMIT, 100_000)


class TestCacheText(unittest.TestCase):
    """_cache_text is the canonical NFKC + 500-char truncation used by
    both the live search path and rebuild_index.py. If either side
    diverges, the on-disk content_hash stops matching the in-memory
    one and the vec-index cache becomes perma-stale."""

    def test_empty_input_returns_empty(self):
        from embedding_search import _cache_text

        self.assertEqual(_cache_text(""), "")

    def test_none_like_input_returns_empty(self):
        from embedding_search import _cache_text

        # Defensive: even falsy values must not raise.
        self.assertEqual(_cache_text(None or ""), "")

    def test_truncates_at_500_chars(self):
        from embedding_search import _cache_text

        s = "x" * 1000
        out = _cache_text(s)
        self.assertEqual(len(out), 500)

    def test_preserves_short_string(self):
        from embedding_search import _cache_text

        self.assertEqual(_cache_text("hello world"), "hello world")

    def test_normalizes_unicode(self):
        from embedding_search import _cache_text

        # Two different Unicode representations of the same character.
        # NFKC must collapse them so embeddings are stable.
        nfc = "café"  # precomposed
        nfd = "cafe\u0301"  # combining acute
        self.assertEqual(_cache_text(nfc), _cache_text(nfd))


class TestParseTags(unittest.TestCase):
    def test_empty_string_returns_empty_list(self):
        from embedding_search import _parse_tags

        self.assertEqual(_parse_tags(""), [])

    def test_none_returns_empty_list(self):
        from embedding_search import _parse_tags

        self.assertEqual(_parse_tags(None), [])

    def test_parses_valid_json_list(self):
        from embedding_search import _parse_tags

        self.assertEqual(_parse_tags('["a", "b"]'), ["a", "b"])

    def test_invalid_json_returns_empty_list(self):
        from embedding_search import _parse_tags

        self.assertEqual(_parse_tags("not json"), [])

    def test_non_list_json_returns_empty_list(self):
        from embedding_search import _parse_tags

        self.assertEqual(_parse_tags('{"a": 1}'), [])


class TestBuildContextPrefix(unittest.TestCase):
    def test_no_inputs_returns_empty(self):
        from embedding_search import _build_context_prefix

        self.assertEqual(_build_context_prefix(), "")

    def test_category_only(self):
        from embedding_search import _build_context_prefix

        self.assertEqual(_build_context_prefix(category="lessons"), "[lessons] ")

    def test_category_and_tags(self):
        from embedding_search import _build_context_prefix

        out = _build_context_prefix(category="lessons", tags=["python", "tests"])
        self.assertIn("lessons", out)
        self.assertIn("python", out)
        self.assertIn("tests", out)
        # Must start with bracket and end with "] ".
        self.assertTrue(out.startswith("["))
        self.assertTrue(out.endswith("] "))

    def test_tag_count_capped_at_five(self):
        from embedding_search import _build_context_prefix

        long_tags = [f"t{i}" for i in range(20)]
        out = _build_context_prefix(tags=long_tags)
        # Only the first 5 tags should appear in the prefix.
        for i in range(5):
            self.assertIn(f"t{i}", out)
        for i in range(5, 20):
            self.assertNotIn(f"t{i}", out)

    def test_source_file_extracts_top_folder(self):
        from embedding_search import _build_context_prefix

        out = _build_context_prefix(source_file="lessons/foo/bar.md")
        self.assertIn("lessons", out)


class TestVecCacheRoundtrip(unittest.TestCase):
    """The vector search cache is global module-level state. Each
    test must reset it via clear_vec_cache() to avoid cross-test
    pollution."""

    def setUp(self):
        from embedding_search import clear_vec_cache

        clear_vec_cache()

    def test_put_then_get_returns_value(self):
        from embedding_search import (
            _vec_cache_get,
            _vec_cache_put,
            clear_vec_cache,
        )

        clear_vec_cache()
        _vec_cache_put(("db1", "query1"), ["hit1", "hit2"])
        self.assertEqual(_vec_cache_get(("db1", "query1")), ["hit1", "hit2"])

    def test_get_missing_key_returns_none(self):
        from embedding_search import _vec_cache_get, clear_vec_cache

        clear_vec_cache()
        self.assertIsNone(_vec_cache_get(("nope",)))

    def test_clear_empties_cache(self):
        from embedding_search import (
            _vec_cache_get,
            _vec_cache_put,
            clear_vec_cache,
        )

        _vec_cache_put(("k",), "v")
        clear_vec_cache()
        self.assertIsNone(_vec_cache_get(("k",)))

    def test_cache_respects_max_size(self):
        """After _VEC_CACHE_MAX inserts, the oldest entry must be evicted."""
        from embedding_search import (
            _VEC_CACHE_MAX,
            _vec_cache_get,
            _vec_cache_put,
            clear_vec_cache,
        )

        clear_vec_cache()
        # Insert max+5 items; the first 5 should be evicted.
        for i in range(_VEC_CACHE_MAX + 5):
            _vec_cache_put((f"k{i}",), f"v{i}")
        # First keys must be gone.
        for i in range(5):
            self.assertIsNone(
                _vec_cache_get((f"k{i}",)),
                f"key k{i} should have been evicted",
            )
        # Recent keys must still be present.
        for i in range(5, _VEC_CACHE_MAX + 5):
            self.assertEqual(_vec_cache_get((f"k{i}",)), f"v{i}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
