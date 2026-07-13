"""PR1.2 -- Single Monotonic CE.

Replaces the dual-CE path (weak stage 9b + chunk stage 9c, both rewriting
``r[6]``) with ONE CE stage selected by query type. These tests prove:

  (a) for a given query type, exactly ONE CE stage rewrote ``r[6]``;
  (b) query-type selection routes short vs long queries to the expected stage;
  (c) rank-lock still holds -- the CE-determined order is never reordered by
      enrichment, and the single-CE output is monotonic (sorted by ``r[6]``).
"""

from __future__ import annotations

import random

import pytest

import search.rerankers as R
import search.enrichment as E
from search.rerankers import (
    _apply_single_ce_rerank,
    _apply_combined_ce_rerank,
    _detect_ce_query_type,
    _select_ce_mode,
)


# -- synthetic result factory -------------------------------------------------


def _mk(n: int, seed: int = 0) -> list:
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        sc = rnd.random()
        rows.append(
            (
                f"n{i:03d}",           # 0 id
                f"content {i}",         # 1 content
                "f.md",                 # 2 source_file
                "[]",                   # 3 tags
                "2024-01-01T00:00:00+00:00",  # 4 created
                i + 1,                  # 5 rank
                sc,                     # 6 final_score
                0.5,                    # 7 fitness
                3,                      # 8 importance
                0,                      # 9 pinned
                None,                   # 10 last_accessed
                None,                   # 11 avg_dist
            )
        )
    return rows


# -- spy installer ------------------------------------------------------------


def _install_ce_spies(monkeypatch, deep_returns=None):
    """Install spies that TAG r[6] with the stage name and record call counts.

    ``deep_returns`` controls what the deep spy returns: a list (success) or
    None (graceful failure -> fallback to weak/chunk).
    """
    calls = {"weak": 0, "chunk": 0, "deep": 0}
    sentinels = {"weak": "WEAK", "chunk": "CHUNK", "deep": "DEEP"}

    def make(kind):
        # Real signatures: _apply_weak_ce_rerank(query, scored_results, top_k, blend)
        # and _apply_ce_chunk_rerank(query, scored_results, top_k, blend).
        # The first positional arg is the query, the results list is second.
        def spy(query, results, *a, **k):
            calls[kind] += 1
            out = []
            for r in results:
                nr = list(r)
                nr[6] = sentinels[kind]
                out.append(tuple(nr))
            return out

        return spy

    monkeypatch.setattr(R, "_apply_weak_ce_rerank", make("weak"))
    monkeypatch.setattr(R, "_apply_ce_chunk_rerank", make("chunk"))

    # _try_deep_rerank(query, scored_results, top_k=30)
    def deep_spy(query, results, *a, **k):
        calls["deep"] += 1
        if deep_returns is None:
            return None  # graceful failure
        out = []
        for r in results:
            nr = list(r)
            nr[6] = sentinels["deep"]
            out.append(tuple(nr))
        return out

    monkeypatch.setattr(R, "_try_deep_rerank", deep_spy)
    return calls


# -- (b) query-type routing ---------------------------------------------------


@pytest.mark.parametrize(
    "query,expected",
    [
        ("python sorting", "weak"),
        ("how to sort a list", "weak"),
        ("def foo(x): return x * 2", "weak"),
        ("```python\nprint(1)\n```", "weak"),
        (
            "What is the best way to configure the memory system when the "
            "database lives on a remote host and the embedding model fails to "
            "load and the reranker times out unexpectedly?",
            "chunk",
        ),
        ("What is X? How does Y work? Why is Z so slow?", "chunk"),
        ("", "weak"),
    ],
)
def test_detect_ce_query_type_routing(query, expected):
    assert _detect_ce_query_type(query) == expected


def test_select_ce_mode_deep_env(monkeypatch):
    monkeypatch.setenv("MEMORY_CE_DEEP", "1")
    assert _select_ce_mode("python sorting") == "deep"


def test_select_ce_mode_deep_param():
    assert _select_ce_mode("python sorting", deep_rerank_param=True) == "deep"


def test_select_ce_mode_routes_by_type():
    # Non-deep path collapses weak+chunk into ONE stage -> "combined".
    assert _select_ce_mode("python sorting") == "combined"
    assert _select_ce_mode("What is X? How does Y work?") == "combined"
    # Deep still selected when explicitly requested.
    assert _select_ce_mode("python sorting", deep_rerank_param=True) == "deep"


# -- (a) exactly one CE stage rewrites r[6] ----------------------------------


def test_single_ce_weak_only(monkeypatch):
    rows = _mk(10, seed=1)
    calls = _install_ce_spies(monkeypatch, deep_returns=[])
    out = _apply_single_ce_rerank("python sorting", rows, top_k=10, mode="weak")
    assert calls["weak"] == 1 and calls["chunk"] == 0 and calls["deep"] == 0
    assert all(r[6] == "WEAK" for r in out)


def test_single_ce_chunk_only(monkeypatch):
    rows = _mk(10, seed=2)
    calls = _install_ce_spies(monkeypatch, deep_returns=[])
    out = _apply_single_ce_rerank("a long conversational query here", rows, top_k=10, mode="chunk")
    assert calls["chunk"] == 1 and calls["weak"] == 0 and calls["deep"] == 0
    assert all(r[6] == "CHUNK" for r in out)


def test_single_ce_deep_success(monkeypatch):
    rows = _mk(10, seed=3)
    calls = _install_ce_spies(monkeypatch, deep_returns=[0.5] * 10)
    out = _apply_single_ce_rerank("python sorting", rows, top_k=10, mode="deep")
    assert calls["deep"] == 1 and calls["weak"] == 0 and calls["chunk"] == 0
    assert all(r[6] == "DEEP" for r in out)


def test_single_ce_deep_falls_back_gracefully(monkeypatch):
    # deep requested but model unavailable -> the combined (weak+chunk)
    # baseline is returned (exactly one r[6] write, never a bare weak/chunk).
    monkeypatch.setattr(R, "_try_deep_rerank", lambda q, res, top_k=30: None)
    monkeypatch.setattr(R, "_get_ce_chunk_model", lambda: _FakeCEModel())

    rows = _mk(10, seed=4)
    out = _apply_single_ce_rerank(
        "python sorting", rows, top_k=10, mode="deep", weak_k=10, chunk_k=10
    )
    base = _apply_combined_ce_rerank("python sorting", rows, weak_k=10, chunk_k=10)
    assert [r[0] for r in out] == [r[0] for r in base]
    assert [round(r[6], 6) for r in out] == [round(r[6], 6) for r in base]

    rows2 = _mk(10, seed=5)
    out2 = _apply_single_ce_rerank(
        "What is X? How does Y work? Why is Z slow?",
        rows2, top_k=10, mode="deep", weak_k=10, chunk_k=10,
    )
    base2 = _apply_combined_ce_rerank(
        "What is X? How does Y work? Why is Z slow?", rows2, weak_k=10, chunk_k=10
    )
    assert [r[0] for r in out2] == [r[0] for r in base2]


# -- (c) rank-lock still holds ----------------------------------------------


def test_single_ce_is_monotonic(monkeypatch):
    # Force the hand-rolled weak CE (no model load) for a hermetic check.
    monkeypatch.setattr(R, "_get_ce_chunk_model", lambda: None)
    rows = _mk(12, seed=6)
    out = _apply_single_ce_rerank("python sorting", rows, top_k=12, mode="weak")
    scores = [float(r[6]) if r[6] is not None else 0.0 for r in out]
    assert scores == sorted(scores, reverse=True)


def test_rank_lock_enrichment_does_not_reorder(monkeypatch):
    monkeypatch.setattr(R, "_get_ce_chunk_model", lambda: None)
    rows = _mk(12, seed=7)
    # Mirror the orchestrator Phase-9 chain: single CE -> late interaction
    # (a separate, non-CE reranker) -> final rank-first sort.
    out = _apply_single_ce_rerank("python sorting", rows, top_k=12, mode="weak")
    out = R._apply_late_interaction_rerank("python sorting", out, top_k=12)
    out = sorted(
        out, key=lambda r: (float(r[6]) if r[6] is not None else 0.0), reverse=True
    )
    ce_order = [r[0] for r in out]
    # Convert to dict items exactly as the orchestrator does, then enrich.
    items = [{"id": r[0], "final_score": r[6]} for r in out]
    enriched = E._apply_post_rank_metadata(items, "python sorting", db_path="/no/such/db")
    assert [it["id"] for it in enriched] == ce_order


# -- static wiring: orchestrator uses the single CE, not the dual path ------


def test_orchestrator_wires_single_ce_once():
    orch_src = (search_orchestrator_path()).read_text(encoding="utf-8")
    # The single CE dispatcher is called exactly once in the rerank function.
    assert orch_src.count("_apply_single_ce_rerank(") == 1
    # The legacy dual-CE path must be gone.
    assert "_apply_cross_encoder_rerank(" not in orch_src
    assert "_apply_ce_chunk_rerank(" not in orch_src
    # late-interaction (a non-CE reranker) still runs once.
    assert orch_src.count("_apply_late_interaction_rerank(") == 1
    # The PR1.1 final rank-first sort is preserved.
    assert "out = sorted(" in orch_src


class _FakeCEModel:
    """Deterministic stand-in for the ms-marco-MiniLM cross-encoder."""

    def predict(self, pairs, show_progress_bar=False, batch_size=128):
        out = []
        for q, d in pairs:
            s = float((len(d) % 11) - 5 + (hash(q) % 3))
            out.append(s)
        return out


def test_combined_ce_matches_sequential_baseline(monkeypatch):
    # The single combined stage must reproduce the PR1.1 sequential
    # weak(0.6) -> chunk(0.7) baseline with identical per-item r[6].
    monkeypatch.setattr(R, "_get_ce_chunk_model", lambda: _FakeCEModel())
    rows = _mk(12, seed=21)
    query = "what did we decide about the deployment pipeline and rollback plan"
    # Sequential baseline = model-weak CE (stage 9b) then chunk CE (stage 9c),
    # the exact two-stage pipeline the combined stage collapses into one write.
    baseline = R._apply_ce_chunk_rerank(
        query, R._apply_weak_ce_rerank(query, list(rows), top_k=8, blend=0.6),
        top_k=12, blend=0.7,
    )
    combined = _apply_combined_ce_rerank(query, list(rows), weak_k=8, chunk_k=12)
    base_map = {r[0]: round(r[6], 6) for r in baseline}
    comb_map = {r[0]: round(r[6], 6) for r in combined}
    assert base_map == comb_map


def test_combined_ce_single_write(monkeypatch):
    # Exactly one r[6] write point, touching every item exactly once.
    monkeypatch.setattr(R, "_get_ce_chunk_model", lambda: _FakeCEModel())
    calls = {"n": 0, "items": 0}
    orig = R._assign_rank_once

    def spy(scored, final_r6, chunk_k):
        calls["n"] += 1
        calls["items"] = len(final_r6)
        return orig(scored, final_r6, chunk_k)

    monkeypatch.setattr(R, "_assign_rank_once", spy)
    rows = _mk(12, seed=22)
    out = _apply_combined_ce_rerank(
        "how do we roll out the new auth service", rows, weak_k=8, chunk_k=12
    )
    assert calls["n"] == 1
    assert calls["items"] == 12
    assert len(out) == 12


def test_combined_ce_deterministic(monkeypatch):
    monkeypatch.setattr(R, "_get_ce_chunk_model", lambda: _FakeCEModel())
    rows = _mk(12, seed=23)
    q = "what changed in the embedding index after the warm-up rebuild"
    a = _apply_combined_ce_rerank(q, rows, weak_k=8, chunk_k=12)
    b = _apply_combined_ce_rerank(q, rows, weak_k=8, chunk_k=12)
    assert [r[0] for r in a] == [r[0] for r in b]
    assert [round(r[6], 6) for r in a] == [round(r[6], 6) for r in b]


def test_combined_ce_applies_both_signals(monkeypatch):
    # The combined r[6] must differ from a pure-weak (no chunk) run, proving
    # the chunk CE signal is actually folded in.
    monkeypatch.setattr(R, "_get_ce_chunk_model", lambda: _FakeCEModel())
    rows = _mk(12, seed=24)
    q = "how should we handle the migration of the legacy postgres database"
    combined = _apply_combined_ce_rerank(q, list(rows), weak_k=8, chunk_k=12)
    weak_only = R._apply_cross_encoder_rerank(q, list(rows), top_k=8)
    comb_map = {r[0]: round(r[6], 6) for r in combined}
    weak_map = {r[0]: round(r[6], 6) for r in weak_only}
    assert comb_map != weak_map


def test_combined_rank_lock(monkeypatch):
    # Mirror orchestrator Phase 9: single CE (combined) -> late interaction
    # -> final rank-first sort. Enrichment must not reorder.
    monkeypatch.setattr(R, "_get_ce_chunk_model", lambda: _FakeCEModel())
    rows = _mk(12, seed=25)
    q = "how should we handle the canary rollout and monitor error rates"
    out = _apply_single_ce_rerank(
        q, rows, top_k=12, mode="combined", weak_k=12, chunk_k=12
    )
    out = R._apply_late_interaction_rerank(q, out, top_k=12)
    out = sorted(
        out, key=lambda r: (float(r[6]) if r[6] is not None else 0.0), reverse=True
    )
    ce_order = [r[0] for r in out]
    items = [{"id": r[0], "final_score": r[6]} for r in out]
    enriched = E._apply_post_rank_metadata(items, q, db_path="/no/such/db")
    assert [it["id"] for it in enriched] == ce_order


def search_orchestrator_path():
    from pathlib import Path

    return Path("search/orchestrator.py")
