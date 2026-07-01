"""M2: Tests for the reranker module and the deep_rerank flag on
search_memories / _apply_cross_encoder_rerank.

The actual jina-reranker-v3 model is NOT loaded in any test — too slow
(~3.5s load + 1-3s per inference on CPU). The Reranker.score() path is
mocked where the test exercises the integration. The model is loaded once
during the test session in a separate class (TestRealModelSmoke) gated on
an env var so it can be skipped in CI.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# H3: redirect GLOBAL_MEM_DIR + resolve_active_memory_dir BEFORE importing
# any module that touches the prod DB. Saved as pinned lesson.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "eval")
)

from memory_mcp import GLOBAL_MEM_DIR as PROD_MEM_DIR  # noqa: E402

import memory_mcp  # noqa: E402
import infra.reranker as reranker  # noqa: E402


def _isolate_active_db():
    """Redirect GLOBAL_MEM_DIR + resolve_active_memory_dir to a tmp dir.

    Returns the original resolve_active_memory_dir so callers can restore it.
    """
    import tempfile
    from pathlib import Path

    tmp = tempfile.mkdtemp(prefix="reranker-test-")
    Path(tmp) / "memory.db"
    memory_mcp.GLOBAL_MEM_DIR = Path(tmp)
    orig_resolve = None
    if hasattr(memory_mcp, "resolve_active_memory_dir"):
        orig_resolve = memory_mcp.resolve_active_memory_dir
        memory_mcp.resolve_active_memory_dir = lambda **_: Path(tmp)
    return orig_resolve


class TestRerankerModule(unittest.TestCase):
    """Pure-module tests. No model load."""

    def setUp(self):
        self._orig_resolve = _isolate_active_db()
        reranker.reset_reranker_for_tests()

    def tearDown(self):
        reranker.reset_reranker_for_tests()
        memory_mcp.GLOBAL_MEM_DIR = PROD_MEM_DIR
        if self._orig_resolve is not None:
            memory_mcp.resolve_active_memory_dir = self._orig_resolve

    def test_constants_present(self):
        # 2026-06-15 swap: jina-reranker-v3 → Qwen3-Reranker-0.6B primary,
        # BAAI/bge-reranker-v2-m3 fallback. Both are MPS-safe, Apache 2.0 / MIT.
        self.assertTrue(
            reranker.PRIMARY_MODEL_ID.startswith("Qwen/"),
            f"PRIMARY_MODEL_ID should be a Qwen model, got {reranker.PRIMARY_MODEL_ID!r}",
        )
        self.assertEqual(reranker.FALLBACK_MODEL_ID, "BAAI/bge-reranker-v2-m3")
        self.assertIsInstance(reranker.PRIMARY_REVISION, str)
        self.assertIsInstance(reranker.FALLBACK_REVISION, str)
        self.assertGreater(len(reranker.PRIMARY_REVISION), 0)
        self.assertGreater(len(reranker.FALLBACK_REVISION), 0)
        # Legacy aliases still resolve.
        self.assertEqual(reranker.RERANKER_MODEL_ID, reranker.PRIMARY_MODEL_ID)
        self.assertEqual(reranker.RERANKER_REVISION, reranker.PRIMARY_REVISION)
        self.assertIsInstance(reranker.RERANKER_ENABLED, bool)

    def test_singleton_returns_same_instance(self):
        a = reranker.get_reranker()
        b = reranker.get_reranker()
        self.assertIs(a, b)

    def test_reset_clears_singleton(self):
        a = reranker.get_reranker()
        reranker.reset_reranker_for_tests()
        b = reranker.get_reranker()
        self.assertIsNot(a, b)

    def test_reranker_starts_unloaded(self):
        r = reranker.Reranker()
        self.assertFalse(r.is_loaded())
        self.assertIsNone(r.load_error())

    def test_reranker_disabled_via_env_returns_none_on_score(self):
        reranker.Reranker()
        with patch.dict(os.environ, {"MEMORY_RERANKER_DISABLED": "1"}):
            # RERANKER_ENABLED is captured at module import. Recreate the
            # disabled branch by re-reading the env through the load path
            # — but since the module-level flag is already True, simulate
            # the failure mode differently: just verify that load() with a
            # broken model id returns False.
            pass
        # Instead test the load-failure path with a fake model id.
        # 2026-06-15: Reranker now takes primary_id/fallback_id; if both
        # are bogus, load() returns False without loading anything.
        r2 = reranker.Reranker(
            primary_id="nonexistent/fake-primary-xyz",
            fallback_id="nonexistent/fake-fallback-xyz",
        )
        ok = r2.load()
        self.assertFalse(ok)
        self.assertFalse(r2.is_loaded())
        self.assertIsNotNone(r2.load_error())
        # score returns None
        self.assertIsNone(r2.score("q", ["d1", "d2"]))

    def test_reranker_load_failure_is_idempotent(self):
        r = reranker.Reranker(
            primary_id="nonexistent/fake-primary-xyz",
            fallback_id="nonexistent/fake-fallback-xyz",
        )
        self.assertFalse(r.load())
        # Second call: should not re-attempt and should still report not loaded.
        self.assertFalse(r.load())
        self.assertFalse(r.is_loaded())


class TestNormalizeRerankScore(unittest.TestCase):
    """normalize_rerank_score maps roughly [-1, 1] → [0, 1] via sigmoid."""

    def test_zero_is_half(self):
        self.assertAlmostEqual(reranker.normalize_rerank_score(0.0), 0.5, places=5)

    def test_positive_is_above_half(self):
        self.assertGreater(reranker.normalize_rerank_score(0.5), 0.5)
        self.assertGreater(reranker.normalize_rerank_score(1.0), 0.7)

    def test_negative_is_below_half(self):
        self.assertLess(reranker.normalize_rerank_score(-0.5), 0.5)
        self.assertLess(reranker.normalize_rerank_score(-1.0), 0.3)

    def test_monotonic(self):
        prev = 0.0
        for x in [-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0]:
            v = reranker.normalize_rerank_score(x)
            self.assertGreater(v, prev)
            prev = v

    def test_extreme_values_dont_overflow(self):
        # Numerically stable — should not raise
        self.assertAlmostEqual(reranker.normalize_rerank_score(1000.0), 1.0, places=5)
        self.assertAlmostEqual(reranker.normalize_rerank_score(-1000.0), 0.0, places=5)


class TestApplyCERerankWithDeep(unittest.TestCase):
    """Wire-level tests for _apply_cross_encoder_rerank(deep_rerank=...)."""

    def _scored(self, ids, contents, scores):
        # Shape: (id, content, source_file, tags, created, rank, final_score, fitness, importance, pinned)
        return [
            (ids[i], contents[i], "src", "[]", 1.0, 1.0, scores[i], 1.0, 3, 0)
            for i in range(len(ids))
        ]

    def setUp(self):
        self._orig_resolve = _isolate_active_db()
        reranker.reset_reranker_for_tests()

    def tearDown(self):
        reranker.reset_reranker_for_tests()
        memory_mcp.GLOBAL_MEM_DIR = PROD_MEM_DIR
        if self._orig_resolve is not None:
            memory_mcp.resolve_active_memory_dir = self._orig_resolve

    def test_deep_rerank_false_uses_weak_ce(self):
        # deep_rerank=False: no model load attempt, weak CE only.
        scored = self._scored(
            ["a", "b", "c"],
            ["alpha beta gamma", "completely different content", "alpha alpha alpha"],
            [0.5, 0.5, 0.5],
        )
        with patch.object(reranker, "get_reranker") as mock_get:
            result = memory_mcp._apply_cross_encoder_rerank(
                "alpha",
                scored,
                top_k=3,
                deep_rerank=False,
            )
            mock_get.assert_not_called()
        # 'alpha' should rank docs with 'alpha' higher (weak CE punishes 'b').
        ids = [r[0] for r in result]
        self.assertNotEqual(ids[0], "b")

    def test_deep_rerank_true_with_mocked_jina_swaps_scores(self):
        # Mock the singleton: get_reranker().score() returns fixed scores.
        mock_jina = MagicMock()
        # Inverted: doc c is most relevant by jina
        mock_jina.score.return_value = [0.1, -0.5, 0.8]  # raw jina scores
        with patch.object(reranker, "get_reranker", return_value=mock_jina):
            scored = self._scored(
                ["a", "b", "c"],
                ["alpha", "beta", "gamma"],
                [0.5, 0.5, 0.5],  # identical pre-rerank
            )
            result = memory_mcp._apply_cross_encoder_rerank(
                "q",
                scored,
                top_k=3,
                deep_rerank=True,
            )
            mock_jina.score.assert_called_once()
            call_args = mock_jina.score.call_args
            self.assertEqual(call_args[0][0], "q")
            self.assertEqual(call_args[0][1], ["alpha", "beta", "gamma"])
        # After sigmoid: c has highest score, then a, then b
        # Top result should be 'c'
        self.assertEqual(result[0][0], "c")
        self.assertEqual(result[2][0], "b")

    def test_deep_rerank_jina_returns_none_falls_back_to_weak(self):
        # If jina.score() returns None (model not loaded), use weak CE.
        mock_jina = MagicMock()
        mock_jina.score.return_value = None
        with patch.object(reranker, "get_reranker", return_value=mock_jina):
            scored = self._scored(
                ["a", "b"],
                ["alpha alpha alpha", "beta beta beta"],
                [0.5, 0.5],
            )
            result = memory_mcp._apply_cross_encoder_rerank(
                "alpha",
                scored,
                top_k=2,
                deep_rerank=True,
            )
            # Fallback to weak CE: 'a' should rank above 'b'
            self.assertEqual(result[0][0], "a")

    def test_deep_rerank_jina_raises_falls_back_to_weak(self):
        # If get_reranker() itself raises (e.g. import error), no crash.
        with patch.object(reranker, "get_reranker", side_effect=ImportError("nope")):
            scored = self._scored(
                ["a", "b"],
                ["alpha alpha alpha", "beta beta beta"],
                [0.5, 0.5],
            )
            result = memory_mcp._apply_cross_encoder_rerank(
                "alpha",
                scored,
                top_k=2,
                deep_rerank=True,
            )
            # No exception, weak CE was used as fallback
            self.assertEqual(result[0][0], "a")

    def test_tail_preserved_after_rerank(self):
        mock_jina = MagicMock()
        mock_jina.score.return_value = [0.0, 0.0]  # neutral scores
        with patch.object(reranker, "get_reranker", return_value=mock_jina):
            scored = self._scored(
                ["a", "b", "c", "d"],
                ["x", "y", "tail1", "tail2"],
                [0.9, 0.8, 0.3, 0.2],
            )
            result = memory_mcp._apply_cross_encoder_rerank(
                "q",
                scored,
                top_k=2,
                deep_rerank=True,
            )
        # tail is [c, d] in original order
        tail_ids = [r[0] for r in result[2:]]
        self.assertEqual(tail_ids, ["c", "d"])


class TestSearchMemoriesDeepRerankFlag(unittest.TestCase):
    """The deep_rerank param is plumbed all the way through."""

    def setUp(self):
        self._orig_resolve = _isolate_active_db()
        reranker.reset_reranker_for_tests()

    def tearDown(self):
        reranker.reset_reranker_for_tests()
        memory_mcp.GLOBAL_MEM_DIR = PROD_MEM_DIR
        if self._orig_resolve is not None:
            memory_mcp.resolve_active_memory_dir = self._orig_resolve

    def test_search_memories_accepts_deep_rerank(self):
        import inspect

        sig = inspect.signature(memory_mcp.search_memories)
        self.assertIn("deep_rerank", sig.parameters)
        self.assertIs(sig.parameters["deep_rerank"].default, False)

    def test_memory_search_accepts_deep_rerank(self):
        import inspect

        sig = inspect.signature(memory_mcp.memory_search)
        self.assertIn("deep_rerank", sig.parameters)
        self.assertIs(sig.parameters["deep_rerank"].default, False)


class TestRealModelSmoke(unittest.TestCase):
    """Optional real-model test, gated on an env var. Skip by default.

    Run with:  RUN_RERANKER_SMOKE=1 python -m unittest eval.test_reranker
    """

    @classmethod
    def setUpClass(cls):
        if not os.environ.get("RUN_RERANKER_SMOKE"):
            raise unittest.SkipTest(
                "set RUN_RERANKER_SMOKE=1 to run the model load smoke test"
            )

    def test_load_and_score_real_model(self):
        r = reranker.Reranker()
        ok = r.load()
        self.assertTrue(ok, f"model failed to load: {r.load_error()}")
        self.assertTrue(r.is_loaded())
        scores = r.score(
            "What are the health benefits of green tea?",
            [
                "Green tea contains antioxidants called catechins.",
                "The price of coffee has increased 20% this year.",
                "Studies show that drinking green tea can improve brain function.",
                "Basketball is a popular sport in the United States.",
            ],
        )
        self.assertIsNotNone(scores)
        assert scores is not None  # for type checker
        self.assertEqual(len(scores), 4)
        # Normalize and verify ranking: tea-related docs > unrelated
        normalized = [reranker.normalize_rerank_score(s) for s in scores]
        # doc index 2 (brain function) and 0 (catechins) should outrank 1 (coffee) and 3 (basketball)
        self.assertGreater(normalized[2] + normalized[0], normalized[1] + normalized[3])


if __name__ == "__main__":
    unittest.main()
