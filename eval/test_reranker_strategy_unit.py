#!/usr/bin/env python3
"""Unit tests for the reranker strategy dispatch (BaseReranker + registry)."""

import sys
import unittest
from pathlib import Path

import pytest

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from search.rerankers import (
    BaseReranker,
    ChunkReranker,
    CombinedReranker,
    DeepReranker,
    WeakReranker,
    get_reranker_strategy,
)


class TestRerankerStrategyDispatch(unittest.TestCase):
    def test_registry_returns_correct_strategy_per_mode(self):
        self.assertIsInstance(get_reranker_strategy("combined"), CombinedReranker)
        self.assertIsInstance(get_reranker_strategy("deep"), DeepReranker)
        self.assertIsInstance(get_reranker_strategy("chunk"), ChunkReranker)
        self.assertIsInstance(get_reranker_strategy("weak"), WeakReranker)

    def test_unknown_mode_falls_back_to_weak(self):
        self.assertIsInstance(get_reranker_strategy("does-not-exist"), WeakReranker)
        # empty / garbage modes must not raise
        self.assertIsInstance(get_reranker_strategy(""), WeakReranker)

    def test_strategies_declare_distinct_modes(self):
        modes = {cls.mode for cls in (CombinedReranker, DeepReranker, ChunkReranker, WeakReranker)}
        self.assertEqual(modes, {"combined", "deep", "chunk", "weak"})

    def test_strategies_are_base_reranker_subclasses(self):
        for cls in (CombinedReranker, DeepReranker, ChunkReranker, WeakReranker):
            self.assertTrue(issubclass(cls, BaseReranker))
            # mode is a class attribute, not the abstract default
            self.assertNotEqual(cls.mode, "base")

    def test_base_reranker_is_abstract(self):
        with self.assertRaises(TypeError):
            BaseReranker()  # type: ignore[abstract]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
