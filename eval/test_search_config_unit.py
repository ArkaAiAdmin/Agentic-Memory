#!/usr/bin/env python3
"""Unit tests for search/config.py (SearchConfig) and the _get_* getters.

Asserts the legacy hardcoded fallback defaults (pre-refactor) match the new
``SearchConfig`` pydantic model, and that every ``_get_*`` accessor now
delegates to ``get_search_config()``.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))


# Config stub lacking every search field so all model fallbacks apply.
class _EmptyConfig:
    def __getattr__(self, name: str):
        raise AttributeError(name)


# The canonical legacy fallback defaults captured before the SearchConfig refactor.
LEGACY_DEFAULTS = {
    "rerank_weights": {
        "bm25": 0.40,
        "fitness": 0.20,
        "importance": 0.15,
        "pinned": 0.10,
        "recency": 0.10,
        "tag_match": 0.05,
    },
    "strong_bm25_threshold": 0.95,
    "rerank_half_life_days": 180.0,
    "cross_encoder_blend": 0.6,
    "late_interaction_blend": 0.3,
    "ce_blend": 0.85,
    "ce_chunk_blend": 0.7,
    "embedding_score_threshold": 0.25,
    "temporal_decay_weight": 0.15,
    "search_compute_budget_ms": 200.0,
}


def _reset_cache():
    from search.config import reload_search_config

    reload_search_config()


def _with_empty_config():
    patcher = patch("infra._lazy_imports.get_config", return_value=_EmptyConfig())
    patcher.start()
    _reset_cache()
    return patcher


class TestSearchConfigDefaults(unittest.TestCase):
    def setUp(self):
        self._patcher = _with_empty_config()

    def tearDown(self):
        self._patcher.stop()
        _reset_cache()

    def test_model_defaults_match_legacy_fallbacks(self):
        from search.config import get_search_config

        cfg = get_search_config()
        for key, expected in LEGACY_DEFAULTS.items():
            self.assertEqual(getattr(cfg, key), expected, msg=f"mismatch on {key}")

    def test_rerank_weights_are_all_floats_sum_to_one(self):
        from search.config import get_search_config

        weights = get_search_config().rerank_weights
        self.assertTrue(all(isinstance(v, float) for v in weights.values()))
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=6)

    def test_getters_delegate_to_model(self):
        from search.config import get_search_config

        from search.orchestrator import _get_embedding_score_threshold
        from search.rerankers import (
            _get_ce_blend,
            _get_ce_chunk_blend,
            _get_cross_encoder_blend,
            _get_late_interaction_blend,
        )
        from search.scoring import (
            _get_rerank_half_life_days,
            _get_rerank_weights,
            _get_strong_bm25_threshold,
        )

        cfg = get_search_config()
        self.assertEqual(_get_rerank_weights(), cfg.rerank_weights)
        self.assertEqual(_get_strong_bm25_threshold(), cfg.strong_bm25_threshold)
        self.assertEqual(_get_rerank_half_life_days(), cfg.rerank_half_life_days)
        self.assertEqual(_get_cross_encoder_blend(), cfg.cross_encoder_blend)
        self.assertEqual(_get_late_interaction_blend(), cfg.late_interaction_blend)
        self.assertEqual(_get_ce_blend(), cfg.ce_blend)
        self.assertEqual(_get_ce_chunk_blend(), cfg.ce_chunk_blend)
        self.assertEqual(_get_embedding_score_threshold(), cfg.embedding_score_threshold)

    def test_custom_config_section_is_read(self):
        from search.config import get_search_config

        class _CustomConfig:
            rerank_weights = {"bm25": 0.9}
            strong_bm25_threshold = 0.5
            rerank_half_life_days = 30.0
            cross_encoder_blend = 0.1
            late_interaction_blend = 0.2
            ce_blend = 0.3
            ce_chunk_blend = 0.4
            embedding_score_threshold = 0.05
            temporal_decay_weight = 0.01
            search_compute_budget_ms = 200.0

        patcher = patch("infra._lazy_imports.get_config", return_value=_CustomConfig())
        patcher.start()
        _reset_cache()
        try:
            cfg = get_search_config()
            self.assertEqual(cfg.rerank_weights, {"bm25": 0.9})
            self.assertEqual(cfg.strong_bm25_threshold, 0.5)
            self.assertEqual(cfg.rerank_half_life_days, 30.0)
            self.assertEqual(cfg.cross_encoder_blend, 0.1)
            self.assertEqual(cfg.late_interaction_blend, 0.2)
            self.assertEqual(cfg.ce_blend, 0.3)
            self.assertEqual(cfg.ce_chunk_blend, 0.4)
            self.assertEqual(cfg.embedding_score_threshold, 0.05)
            self.assertEqual(cfg.temporal_decay_weight, 0.01)
            self.assertEqual(cfg.search_compute_budget_ms, 200.0)
        finally:
            patcher.stop()
            _reset_cache()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
