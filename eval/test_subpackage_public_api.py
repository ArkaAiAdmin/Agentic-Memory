"""Regression tests: subpackage public API must expose the primary entry-points.

Verifies that ``from search import search_memories`` and
``from save import save_memory`` both resolve, so that a future
refactor that accidentally removes the lazy proxy is caught
immediately in CI.  (S3 audit finding, 2026-06-20)
"""

import importlib

import pytest


def test_search_subpackage_exposes_search_memories():
    """``from search import search_memories`` resolves to the shim."""
    mod = importlib.import_module("search")
    fn = mod.search_memories
    assert callable(fn), f"search_memories is {type(fn)}"


def test_save_subpackage_exposes_save_memory():
    """``from save import save_memory`` resolves to the shim."""
    mod = importlib.import_module("save")
    fn = mod.save_memory
    assert callable(fn), f"save_memory is {type(fn)}"


def test_search_subpackage_exposes_compute_channel_weights():
    """``from search import compute_channel_weights`` resolves (already re-exported)."""
    mod = importlib.import_module("search")
    fn = mod.compute_channel_weights
    assert callable(fn), f"compute_channel_weights is {type(fn)}"


def test_search_bb2_turns_live_link():
    """``search._BB2_TURNS`` is the same list object as ``search.synthesis._BB2_TURNS``."""
    from search import synthesis

    assert (
        __import__("search", fromlist=["_BB2_TURNS"])._BB2_TURNS is synthesis._BB2_TURNS
    )


def test_search_ctr_cache_proxy():
    """Writing ``search_pipeline._CTR_WEIGHTS_CACHE = None`` (via
    ``_ProxyModule`` on the shim) clears ``search.scoring._CTR_WEIGHTS_CACHE``
    so test reset patterns work.

    Note: ``search._CTR_WEIGHTS_CACHE = None`` (the ``search/__init__``
    package) only creates a shadow attribute on the ``search`` package
    itself; the proxy that actually forwards to ``search.scoring`` lives
    on the ``search_pipeline`` shim module.  Tests should reset via
    ``search_pipeline`` to reach the live cache.
    """
    from search import scoring
    import search_pipeline as sp

    scoring._CTR_WEIGHTS_CACHE = ("sentinel", None, True)
    sp._CTR_WEIGHTS_CACHE = None
    assert scoring._CTR_WEIGHTS_CACHE is None
