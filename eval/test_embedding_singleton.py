#!/usr/bin/env python3
"""Unit tests for embedding_search.py singleton + L2 norm assertion.

Run with:
    ~/.config/agentic-memory/venv/bin/python -m unittest eval.test_embedding_singleton -v
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Make the install dir importable (same pattern as test_arc_cache.py)
INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

import embedding_search  # noqa: E402
from embedding_search import EmbeddingSearch, get_embedding_search  # noqa: E402


class TestEmbeddingSingleton(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Warm the singleton once so individual tests measure steady-state
        # behavior. The warmup is paid in setUpClass rather than setUp so
        # the timing test (test_get_singleton_warm_start_under_50ms) sees a
        # cached instance.
        cls.singleton = get_embedding_search()
        if cls.singleton.model is None:
            raise unittest.SkipTest(
                "model2vec/numpy not available in venv; skipping model tests"
            )

    def setUp(self):
        # Each test gets a clean temp dir even if it does not use it.
        self.tmpdir = tempfile.mkdtemp(prefix="embedding_singleton_test_")

    def tearDown(self):
        try:
            for p in Path(self.tmpdir).glob("*"):
                p.unlink()
            Path(self.tmpdir).rmdir()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Singleton identity
    # ------------------------------------------------------------------
    def test_singleton_returns_same_instance(self):
        a = get_embedding_search()
        b = get_embedding_search()
        self.assertIs(a, b, "get_embedding_search() must return the same instance")

    def test_singleton_thread_safe(self):
        # Reset the module singleton so we can observe the race directly.
        # 10 threads race to be the first caller; the lock + double-check
        # guarantees exactly one EmbeddingSearch is constructed and all
        # threads see the same instance.
        original = embedding_search._es_singleton
        instances = []
        errors = []
        barrier = threading.Barrier(10)

        def worker():
            try:
                barrier.wait(timeout=5)
                instances.append(embedding_search.get_embedding_search())
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        try:
            embedding_search._es_singleton = None
            threads = [threading.Thread(target=worker) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
        finally:
            # Restore the warmed singleton so subsequent tests are unaffected.
            embedding_search._es_singleton = original

        self.assertFalse(errors, f"thread workers raised: {errors}")
        self.assertEqual(len(instances), 10)
        first = instances[0]
        for inst in instances[1:]:
            self.assertIs(inst, first, "all threads must observe the same singleton")

    # ------------------------------------------------------------------
    # L2 norm assertion
    # ------------------------------------------------------------------
    def test_l2_norm_assertion_passes_on_normal_text(self):
        es = get_embedding_search()
        with patch.dict(os.environ, {"AGENTIC_MEMORY_DEBUG_NORMS": "1"}):
            vecs = es.encode(["hello world", "test"])
        self.assertIsNotNone(vecs)
        # setUpClass guarantees the model is loaded, so vecs is an ndarray.
        assert vecs is not None
        self.assertEqual(vecs.shape[0], 2)
        import numpy as np
        self.assertTrue(np.allclose(np.linalg.norm(vecs, axis=1), 1.0, atol=0.01))

    def test_l2_norm_assertion_passes_on_empty_list(self):
        es = get_embedding_search()
        with patch.dict(os.environ, {"AGENTIC_MEMORY_DEBUG_NORMS": "1"}):
            vecs = es.encode([])
        self.assertIsNotNone(vecs)
        assert vecs is not None
        self.assertEqual(vecs.shape[0], 0)

    def test_l2_norm_assertion_fires_on_bad_data(self):
        # Build a fresh EmbeddingSearch instance to avoid polluting the
        # singleton's model, then monkeypatch its model.encode to return
        # a vector that is clearly not unit-norm.
        es = EmbeddingSearch()
        if es.model is None:
            self.skipTest("model not loaded")
        import numpy as np
        # L2 norm of [2,2,2,...] with 256 dims = 32 — far from 1.0.
        bad = np.full((1, es.model.dim), 2.0, dtype=np.float32)
        with patch.dict(os.environ, {"AGENTIC_MEMORY_DEBUG_NORMS": "1"}):
            with patch.object(es.model, "encode", return_value=bad):
                with self.assertRaises(ValueError) as ctx:
                    es.encode(["hello"])
        self.assertIn("norm", str(ctx.exception).lower())

    # ------------------------------------------------------------------
    # Backward compatibility
    # ------------------------------------------------------------------
    def test_direct_construction_still_works(self):
        # memory_mcp.py, contradiction_detector.py, eval/perf_envelope.py
        # all do `EmbeddingSearch()` directly. Make sure that path still
        # returns a usable object with the attributes they rely on.
        es = EmbeddingSearch()
        self.assertIsNotNone(es)
        self.assertIsNotNone(es.model, "direct construction must still load the model")
        self.assertIsNotNone(es.np, "direct construction must still expose numpy as .np")
        self.assertTrue(hasattr(es, "encode"), "must still expose .encode()")
        self.assertTrue(hasattr(es, "search"), "must still expose .search()")

    # ------------------------------------------------------------------
    # Hot-path performance
    # ------------------------------------------------------------------
    def test_get_singleton_warm_start_under_50ms(self):
        # The contract: first call may be slow (model load), but every
        # subsequent call should be a near-free None-check. setUpClass
        # already paid the cold-start cost, so this measures the warm path.
        # We call once to ensure caching, then time N subsequent calls.
        get_embedding_search()  # ensure cached
        samples = []
        for _ in range(20):
            t0 = time.perf_counter()
            get_embedding_search()
            samples.append((time.perf_counter() - t0) * 1000.0)
        median = sorted(samples)[len(samples) // 2]
        self.assertLess(
            median, 50.0,
            f"warm get_embedding_search() median should be <50ms, got {median:.3f}ms "
            f"(samples: {[f'{s:.2f}' for s in samples[:5]]}...)"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
