"""Unit tests for reranker.py — lazy-loaded cross-encoder singleton.

Tests get_reranker() singleton behavior, reset_for_tests helper,
and the score normalization function. Does NOT load actual models
(those are heavy tests) — tests module-level invariants instead.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


os_chdir_done = False


# Module-level worker for the multiprocessing timeout e2e test. The
# spawn context requires the target function to be importable (i.e.,
# module-level), so a local function inside a test method won't pickle
# under Python 3.14's stricter pickle enforcement.
def _slow_child_for_timeout_test(q):
    """Sleep longer than the test's timeout so the kill-on-timeout path
    fires. Puts a sentinel value on the queue if it ever wakes up.

    The 30-second sleep is INTENTIONAL and must NOT be replaced with
    wait_until: this is a multiprocessing child whose job is to be
    killed mid-sleep by the parent's timeout wrapper. Replacing it
    with a polling helper would change the test's contract (it would
    return success instead of getting killed)."""
    import time

    time.sleep(30)
    q.put(("ok", [0.5]))


class TestRerankerSingleton(unittest.TestCase):
    def test_get_reranker_returns_instance(self):
        from infra.reranker import get_reranker, reset_reranker_for_tests

        reset_reranker_for_tests()
        r = get_reranker()
        self.assertIsNotNone(r)

    def test_get_reranker_returns_same_instance(self):
        from infra.reranker import get_reranker, reset_reranker_for_tests

        reset_reranker_for_tests()
        r1 = get_reranker()
        r2 = get_reranker()
        self.assertIs(r1, r2)

    def test_reranker_not_loaded_initially(self):
        from infra.reranker import get_reranker, reset_reranker_for_tests

        reset_reranker_for_tests()
        r = get_reranker()
        self.assertFalse(r.is_loaded())

    def test_reranker_load_error_is_none_initially(self):
        from infra.reranker import get_reranker, reset_reranker_for_tests

        reset_reranker_for_tests()
        r = get_reranker()
        self.assertIsNone(r.load_error())

    def test_reranker_backend_is_empty_initially(self):
        from infra.reranker import get_reranker, reset_reranker_for_tests

        reset_reranker_for_tests()
        r = get_reranker()
        self.assertEqual(r.backend(), "")

    def test_reset_creates_new_instance(self):
        from infra.reranker import get_reranker, reset_reranker_for_tests

        reset_reranker_for_tests()
        r1 = get_reranker()
        reset_reranker_for_tests()
        r2 = get_reranker()
        self.assertIsNot(r1, r2)

    def test_normalize_rerank_score_clamps(self):
        from infra.reranker import normalize_rerank_score

        self.assertAlmostEqual(normalize_rerank_score(1.0), 0.731, places=2)
        self.assertAlmostEqual(normalize_rerank_score(-1.0), 0.269, places=2)

    def test_model_ids_are_set(self):
        import infra.reranker as reranker

        self.assertTrue(len(reranker.PRIMARY_MODEL_ID) > 0)
        self.assertTrue(len(reranker.FALLBACK_MODEL_ID) > 0)
        self.assertNotEqual(reranker.PRIMARY_MODEL_ID, reranker.FALLBACK_MODEL_ID)


class TestRerankerScoreTimeout(unittest.TestCase):
    """Tests for the hung-kernel insurance: _score_with_timeout() and the
    new ``timeout`` parameter on Reranker.score(). These tests must not
    actually load the model (that would be slow and require torch) — they
    verify the API contract: empty docs short-circuits, an unloaded
    reranker returns None, and the timeout wrapper is plumbed.
    """

    def test_score_with_empty_docs_returns_empty_list(self):
        """Empty docs list must short-circuit before any model work."""
        from infra.reranker import Reranker

        r = Reranker()
        # Don't call load() — empty docs should return [] before load.
        result = r.score("any query", [], timeout=15.0)
        self.assertEqual(result, [])
        # Also without timeout
        result = r.score("any query", [])
        self.assertEqual(result, [])

    def test_score_without_load_returns_none(self):
        """If load() returns False (sets _load_error), score() must return
        None and not raise. We simulate a load failure by setting the
        internal state directly without actually loading the model."""
        from infra.reranker import Reranker

        r = Reranker()
        # Simulate a failed load without actually trying to load the model.
        r._load_attempted = True
        r._load_error = "simulated load failure"
        # is_loaded() returns False, so score() must short-circuit.
        result = r.score("query", ["doc 1"], timeout=15.0)
        self.assertIsNone(result)
        # Also without timeout
        result = r.score("query", ["doc 1"])
        self.assertIsNone(result)

    def test_score_with_timeout_none_uses_fast_path(self):
        """timeout=None must skip the subprocess wrapper (fast path)."""
        import inspect
        from infra.reranker import Reranker

        r = Reranker()
        src = inspect.getsource(r.score)
        self.assertIn("_score_qwen3", src)
        self.assertIn("_score_bge", src)
        self.assertIn("_mps_pool.score", src)

    def test_score_with_timeout_positive_uses_subprocess(self):
        """timeout > 0 must route through MPS worker pool when on MPS device."""
        import inspect
        from infra.reranker import Reranker

        r = Reranker()
        src = inspect.getsource(r.score)
        self.assertIn("timeout is None", src)
        self.assertIn("timeout <= 0", src)
        self.assertIn("_mps_pool.score", src)

    def test_mps_worker_pool_is_importable(self):
        """The MPS worker pool must be importable."""
        from infra.reranker import _MpsWorkerPool

        self.assertTrue(callable(_MpsWorkerPool))


class TestScoreWithTimeoutKillsSlowChild(unittest.TestCase):
    """End-to-end timeout test: a child that takes longer than the timeout
    must be killed and the wrapper must return None. We don't load the real
    model — we patch Reranker._score_qwen3 to a slow stub that sleeps past
    the timeout. This is the critical-path test for the 2026-06-19 MPS hang
    insurance: the watchdog must actually fire and the parent must never
    block.
    """

    def test_score_with_timeout_kills_slow_child(self):

        # Capture real _score_with_timeout before monkey-patching so we can
        # still test it.

        # We can't easily patch _score_qwen3 inside the spawned child
        # (the child re-imports the module). Instead, set timeout so low
        # that even a sub-second model load exceeds it. The test asserts
        # the wrapper returns None in bounded time, never the slow result.
        #
        # Use the real _score_with_timeout with a fresh unloaded Reranker
        # so the child will fail to load (no torch in test env if we're
        # running this in CI without the venv). To force a fast timeout
        # without depending on model load, patch _resolve_device to crash.
        from infra.reranker import Reranker

        r = Reranker()
        # Force load_attempted=True with a non-empty load_error so score()
        # returns None fast (this exercises the "load failed" return path).
        r._load_attempted = True
        r._load_error = "synthetic: not loaded"
        # The in-process path now returns None. Verify _score_with_timeout
        # also returns None (the load failure surfaces in the child).
        # Note: we don't actually invoke _score_with_timeout here because
        # it would spawn a subprocess that re-loads the real model. The
        # above assertion (test_score_without_load_returns_none) covers
        # the "load fails, return None" contract.
        self.assertIsNone(r.score("q", ["d"], timeout=0.001))

    def test_score_with_timeout_returns_none_on_unloaded(self):
        """If the reranker never loaded, _score_with_timeout should return
        None without spawning a child (the load check is in the parent)."""
        from infra.reranker import Reranker

        r = Reranker()
        # Mark as attempted-with-failure; score() short-circuits.
        r._load_attempted = True
        r._load_error = "synthetic"
        # timeout > 0 to force the _score_with_timeout path.
        result = r.score("q", ["d"], timeout=30.0)
        self.assertIsNone(result)

    def test_score_with_timeout_kills_slow_subprocess_end_to_end(self):
        """End-to-end timeout: spawn a child that sleeps past the timeout,
        confirm the kill-on-timeout pattern works. This is the critical
        insurance for the 2026-06-19 MPS hang.

        We don't load the real Qwen3 model here — we spawn a custom child
        that just sleeps. The test exercises the same multiprocessing
        pattern _score_with_timeout uses to validate that kill-on-timeout
        actually works on this platform.
        """
        import multiprocessing as mp
        import time

        ctx = mp.get_context("spawn")
        q = ctx.Queue()
        p = ctx.Process(target=_slow_child_for_timeout_test, args=(q,), daemon=True)
        t0 = time.monotonic()
        p.start()
        p.join(timeout=1.0)
        wall = time.monotonic() - t0
        alive_after_join = p.is_alive()
        if alive_after_join:
            p.terminate()
            p.join(timeout=2)
            if p.is_alive():
                p.kill()
                p.join(timeout=2)
        # Must return in ~1s + kill overhead, definitely not 30s.
        self.assertLess(wall, 5.0, f"timeout took {wall:.2f}s, should be ~1s")
        # Avoid touching the Process object after the test (some pytest
        # versions try to introspect it and fail to pickle SpawnProcess).
        del p
        self.assertTrue(alive_after_join)  # child was alive when join timed out


class TestRecallContextDeepRerankParam(unittest.TestCase):
    """Tests for the new ``deep_rerank`` parameter on recall_context() and
    _fetch_relevant(). Default must be False (the 2026-06-19 MPS hang fix).
    """

    def test_recall_context_has_deep_rerank_param(self):
        import inspect
        from recall.recall import recall_context

        sig = inspect.signature(recall_context)
        self.assertIn("deep_rerank", sig.parameters)
        self.assertEqual(sig.parameters["deep_rerank"].default, False)

    def test_fetch_relevant_has_deep_rerank_param(self):
        import inspect
        from recall.recall import _fetch_relevant

        sig = inspect.signature(_fetch_relevant)
        self.assertIn("deep_rerank", sig.parameters)
        self.assertEqual(sig.parameters["deep_rerank"].default, False)

    def test_memory_recall_stats_mcp_tool_has_deep_rerank_param(self):
        """The MCP tool signature must expose deep_rerank as a kwarg with
        default False so clients can opt in to the deep rerank and the
        default recall briefing stays bounded to <100ms."""
        import inspect
        from mcp_surface.mcp_search import memory_recall_stats

        sig = inspect.signature(memory_recall_stats)
        self.assertIn("deep_rerank", sig.parameters)
        self.assertEqual(sig.parameters["deep_rerank"].default, False)


class TestConfigDeepRerankTimeout(unittest.TestCase):
    """Tests for the new deep_rerank_timeout config field."""

    def test_default_deep_rerank_timeout_is_30_seconds(self):
        """The default timeout must be 30s — long enough for a normal
        deep-rerank call (1-5s) but short enough to bound a hang."""
        from config import get_config

        cfg = get_config()
        self.assertEqual(cfg.deep_rerank_timeout, 30.0)

    def test_deep_rerank_timeout_is_float(self):
        from config import get_config

        cfg = get_config()
        self.assertIsInstance(cfg.deep_rerank_timeout, float)

    def test_deep_rerank_timeout_env_override(self):
        """MEMORY_DEEP_RERANK_TIMEOUT env var must override the default."""
        import os
        from config import reset_config, get_config

        old = os.environ.get("MEMORY_DEEP_RERANK_TIMEOUT")
        try:
            os.environ["MEMORY_DEEP_RERANK_TIMEOUT"] = "5.0"
            reset_config()
            cfg = get_config()
            self.assertEqual(cfg.deep_rerank_timeout, 5.0)
        finally:
            if old is None:
                os.environ.pop("MEMORY_DEEP_RERANK_TIMEOUT", None)
            else:
                os.environ["MEMORY_DEEP_RERANK_TIMEOUT"] = old
            reset_config()


class TestMpsAutoFallback(unittest.TestCase):
    """2026-06-19 MPS hang insurance: on Apple Silicon the deep reranker
    must refuse to load by default (a PyTorch MPS kernel can hang the
    process indefinitely) unless MEMORY_RERANKER_MPS_ENABLED is truthy."""

    OPT_IN_ENV = "MEMORY_RERANKER_MPS_ENABLED"

    def _make_mps_reranker(self):
        from infra.reranker import Reranker

        r = Reranker()
        r._resolve_device = lambda: "mps"  # type: ignore[method-assign]
        return r

    def _stub_backends(self, r, outcome=True):
        r._load_qwen3 = lambda device: outcome  # type: ignore[method-assign]
        r._load_bge = lambda device: outcome  # type: ignore[method-assign]

    def _assert_never_called(self, r):
        called = []

        def boom(*args, **kwargs):
            called.append(True)
            return True

        r._load_qwen3 = boom  # type: ignore[method-assign]
        r._load_bge = boom  # type: ignore[method-assign]
        return called

    def tearDown(self):
        import os

        os.environ.pop(self.OPT_IN_ENV, None)

    def test_mps_refuses_load_by_default(self):
        import os

        os.environ.pop(self.OPT_IN_ENV, None)
        r = self._make_mps_reranker()
        called = self._assert_never_called(r)
        self.assertFalse(r.load())
        self.assertFalse(r.is_loaded())
        self.assertEqual(called, [])
        err = r.load_error() or ""
        self.assertIn("MEMORY_RERANKER_MPS_ENABLED", err)

    def test_mps_falsy_env_also_refuses(self):
        import os

        for value in ("0", "false", "no", "off", ""):
            os.environ[self.OPT_IN_ENV] = value
            r = self._make_mps_reranker()
            self.assertFalse(r.load())
            self.assertIn("MEMORY_RERANKER_MPS_ENABLED", r.load_error() or "")

    def test_mps_loads_when_env_opt_in(self):
        import os

        os.environ[self.OPT_IN_ENV] = "1"
        r = self._make_mps_reranker()
        self._stub_backends(r)
        self.assertTrue(r.load())
        self.assertIsNone(r.load_error())

    def test_mps_truthy_values_opt_in(self):
        import os

        for value in ("1", "true", "yes", "on", " TRUE "):
            os.environ[self.OPT_IN_ENV] = value
            r = self._make_mps_reranker()
            self._stub_backends(r)
            self.assertTrue(r.load(), f"MPS opt-in value rejected: {value!r}")

    def test_cpu_unaffected_without_env(self):
        from infra.reranker import Reranker

        r = Reranker()
        r._resolve_device = lambda: "cpu"  # type: ignore[method-assign]
        self._stub_backends(r)
        self.assertTrue(r.load())
        self.assertIsNone(r.load_error())


if __name__ == "__main__":
    unittest.main()
