#!/usr/bin/env python3
"""Unit tests for embedding_incremental.py — SsmEncoder, incremental_embed_update, merge_embeddings.

SKIPPED (2026-06-29): SSM v1 removed as a dead end. The new Temporal SSM
lives in search/scoring.py. These tests are kept as a reminder of what
was removed.
"""

import sys
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

import pytest

pytestmark = pytest.mark.skip(reason="SSM v1 removed; v2 in search/scoring.py")

try:
    from embedding_incremental import (  # noqa: E402
        SsmEncoder,
        clear_ssm_cache,
        get_default_encoder,
        incremental_embed_update,
        merge_embeddings,
        reset_default_encoder,
    )
except ImportError:
    pass


class TestSsmEncoderBasic(unittest.TestCase):
    """The bare SsmEncoder API: encode, encode_update, merge."""

    def setUp(self):
        clear_ssm_cache()
        reset_default_encoder()

    def test_encode_returns_128_dim(self):
        e = SsmEncoder()
        v = e.encode("hello world")
        self.assertEqual(len(v), 128)
        self.assertTrue(all(isinstance(x, float) for x in v))

    def test_encode_update_extends_state(self):
        e = SsmEncoder()
        v = e.encode("hello world")
        v2 = e.encode_update(v, "updated text")
        self.assertEqual(len(v2), 128)
        # The updated state must differ from the original
        # (we added new tokens, so the recurrence moved).
        self.assertNotEqual(v, v2)

    def test_merge_averages_states(self):
        e = SsmEncoder()
        v1 = e.encode("first text")
        v2 = e.encode("second text")
        merged = e.merge([v1, v2])
        self.assertEqual(len(merged), 128)

    def test_merge_empty(self):
        e = SsmEncoder()
        merged = e.merge([])
        self.assertEqual(len(merged), 128)
        # Empty merge = zero vector
        self.assertTrue(all(x == 0.0 for x in merged))

    def test_deterministic(self):
        e = SsmEncoder()
        v1 = e.encode("deterministic test")
        v2 = e.encode("deterministic test")
        self.assertEqual(v1, v2)

    def test_encoder_dimension(self):
        e = SsmEncoder(dim=64)
        v = e.encode("anything")
        self.assertEqual(len(v), 64)


class TestIncrementalEmbedUpdate(unittest.TestCase):
    """The public API: incremental_embed_update with and without old_state."""

    def setUp(self):
        clear_ssm_cache()
        reset_default_encoder()

    def test_fresh_encode_no_state(self):
        state = incremental_embed_update("mem/abc", "new content")
        self.assertEqual(len(state), 128)

    def test_update_with_state_returns_extended(self):
        e = get_default_encoder()
        old = e.encode("starting text")
        new = incremental_embed_update("mem/abc", "more text", old_state=old)
        self.assertEqual(len(new), 128)
        # Extending with more text should produce a different vector
        self.assertNotEqual(old, new)

    def test_update_different_text_different_vector(self):
        old = incremental_embed_update("mem/a", "alpha")
        new1 = incremental_embed_update("mem/a", "beta", old_state=old)
        new2 = incremental_embed_update("mem/a", "gamma", old_state=old)
        self.assertNotEqual(new1, new2)


class TestMergeEmbeddings(unittest.TestCase):
    """The merge_embeddings function — both call signatures."""

    def setUp(self):
        clear_ssm_cache()
        reset_default_encoder()

    def test_merge_with_explicit_states(self):
        e = get_default_encoder()
        v1 = e.encode("first")
        v2 = e.encode("second")
        merged = merge_embeddings(["mem/a", "mem/b"], states=[v1, v2])
        self.assertEqual(len(merged), 128)
        # Merged vector must differ from either input
        self.assertNotEqual(merged, v1)
        self.assertNotEqual(merged, v2)

    def test_merge_no_states_returns_zeros(self):
        # No DB → no states loadable → zero vector.
        merged = merge_embeddings(["mem/nonexistent"])
        self.assertEqual(len(merged), 128)
        self.assertTrue(all(x == 0.0 for x in merged))

    def test_merge_empty_input(self):
        merged = merge_embeddings([])
        self.assertEqual(len(merged), 128)
        self.assertTrue(all(x == 0.0 for x in merged))

    def test_merge_three_states(self):
        e = get_default_encoder()
        v1 = e.encode("a")
        v2 = e.encode("b")
        v3 = e.encode("c")
        merged = merge_embeddings(["m1", "m2", "m3"], states=[v1, v2, v3])
        self.assertEqual(len(merged), 128)


class TestDefaultEncoderSingleton(unittest.TestCase):
    """The module-level singleton is stable across calls."""

    def test_singleton_returns_same_instance(self):
        e1 = get_default_encoder()
        e2 = get_default_encoder()
        self.assertIs(e1, e2)

    def test_reset_clears_singleton(self):
        e1 = get_default_encoder()
        reset_default_encoder()
        e2 = get_default_encoder()
        self.assertIsNot(e1, e2)

    def test_singleton_produces_stable_vectors(self):
        e = get_default_encoder()
        v1 = e.encode("singleton test")
        v2 = get_default_encoder().encode("singleton test")
        self.assertEqual(v1, v2)


if __name__ == "__main__":
    unittest.main()
