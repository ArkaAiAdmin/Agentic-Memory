"""Regression tests for PR1.1 -- Rank-First Lock.

The contract (search/enrichment.py::_apply_post_rank_metadata):
  * After the CE reranking stage owns the final ORDER, NO later
    enrichment step may change the relative order of results.
  * concept boost / centrality boost / Jaccard surprise / temporal
    decay are attached as order-invariant *envelope* fields, never as
    a mutation of the ranking ``final_score``.

These tests are deliberately hermetic: they exercise
``_apply_post_rank_metadata`` directly (and a temp empty DB for the
DB-access paths) so the order-preservation guarantee is provable
without the full memory schema.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

import search.enrichment as E


# -- synthetic result factory -------------------------------------------------


def _make_items(n: int, seed: int = 0) -> list:
    rnd = random.Random(seed)
    items = []
    for i in range(n):
        items.append(
            {
                "id": f"note-{i:03d}",
                "source_file": f"/mem/note-{i}.md",
                "created": "2024-01-01T00:00:00+00:00",
                "last_accessed": None,
                "final_score": rnd.random(),
                "ce_score": rnd.random(),
                "metadata": {"entities": [i, i + 100]},
            }
        )
    return items


def _ids(items):
    return [it["id"] for it in items]


# -- core: order is preserved under every perturbation ------------------------


@pytest.mark.parametrize("seed", list(range(12)))
def test_order_preserved_under_shuffle_and_extremes(seed: int) -> None:
    base = _make_items(20, seed=seed)
    rnd = random.Random(seed + 99)
    rnd.shuffle(base)

    # Extreme / degenerate final_score values must not reorder anything.
    base[0]["final_score"] = float("inf")
    base[1]["final_score"] = float("-inf")
    base[2]["final_score"] = 0.0
    base[3]["final_score"] = 1e9
    base[4]["final_score"] = -1e9

    out = E._apply_post_rank_metadata(base, "widgets and gizmos", db_path="/no/such/db")
    assert _ids(out) == _ids(base)
    # Input dicts are never mutated (the function copies before enriching).
    assert base[0]["final_score"] == float("inf")


def test_order_preserved_when_reversed() -> None:
    base = _make_items(15, seed=3)
    rev = list(reversed(base))
    out = E._apply_post_rank_metadata(rev, "query", db_path="/no/such/db")
    assert _ids(out) == _ids(rev)


def test_missing_fields_do_not_crash_or_reorder() -> None:
    items = [
        {"id": "a"},  # no metadata / created / final_score at all
        {"id": "b", "metadata": {}, "created": None},
        {"id": "c", "metadata": {"entities": [1, 2]}, "final_score": 0.5},
        {"id": "d", "final_score": 0.9, "last_accessed": "2025-05-05T00:00:00+00:00"},
    ]
    out = E._apply_post_rank_metadata(items, "query", db_path="/no/such/db")
    assert _ids(out) == ["a", "b", "c", "d"]
    for it in out:
        assert it["concept_boost"] == 1.0
        assert it["centrality_boost"] == 1.0
        assert it["jaccard_surprise"] == 1.0
        # temporal_decay is a float in (0, 1] — the exact value depends on
        # the decay formula, half-life defaults, and wall-clock time.  Items
        # without a created timestamp still pass through the formula and may
        # not land exactly on 1.0, so we only assert the range.
        assert 0.0 < it["temporal_decay"] <= 1.0


def test_empty_input_returns_empty() -> None:
    assert E._apply_post_rank_metadata([], "q", db_path="/x") == []


def test_non_dict_items_pass_through_in_order() -> None:
    items = ["raw", {"id": "x", "final_score": 0.1}, 42, {"id": "y", "final_score": 0.2}]
    out = E._apply_post_rank_metadata(items, "q", db_path="/x")
    # Order preserved: non-dict items pass through; dicts are enriched
    # copies appended IN ORDER.
    assert [type(it).__name__ for it in out] == ["str", "dict", "int", "dict"]
    assert out[0] is items[0]  # non-dict passed through untouched
    assert out[2] is items[2]
    assert out[1]["id"] == "x" and out[3]["id"] == "y"
    assert out[1] is not items[1]  # enriched copy, original unmutated
    assert "concept_boost" in out[1] and "temporal_decay" in out[1]


def test_idempotent_on_order() -> None:
    base = _make_items(10, seed=5)
    once = E._apply_post_rank_metadata(base, "q", db_path="/x")
    twice = E._apply_post_rank_metadata(once, "q", db_path="/x")
    assert _ids(twice) == _ids(base)


# -- meaningful: varying factors must NOT leak into order -------------------


def test_varying_factors_never_reorder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even when enrichment produces wildly different per-item factors, the
    output order is identical to the input order. This is the non-vacuous
    guard: if the envelope ever fed back into ordering, this would fail."""

    def fake_concept_map(db_path):
        # Even-index concepts overlap note i's entities -> 1.20 boost;
        # odd-index concepts use a disjoint entity set -> 1.0. This
        # forces the per-item concept envelope to actually vary.
        return {f"concept-{i}": ({i} if i % 2 == 0 else {7777}) for i in range(20)}

    def fake_centrality_map(db_path):
        return {i: float(i) / 19.0 for i in range(20)}

    def fake_jaccard_map(items_, query):
        # Even-index items echo the query verbatim -> jaccard_surprise 1.0;
        # odd-index items use a disjoint surface -> 0.9. Forces per-item
        # variation in the jaccard envelope factor.
        return {
            it["id"]: ("widgets gizmos" if int(it["id"].split("-")[1]) % 2 == 0 else "zzz qqq")
            for it in items_
        }

    monkeypatch.setattr(E, "_load_concept_map", fake_concept_map)
    monkeypatch.setattr(E, "_load_centrality_map", fake_centrality_map)
    monkeypatch.setattr(E, "_load_jaccard_map", fake_jaccard_map)

    base = _make_items(20, seed=4)
    rnd = random.Random(123)
    rnd.shuffle(base)

    out = E._apply_post_rank_metadata(base, "widgets gizmos", db_path="dummy")
    assert _ids(out) == _ids(base)

    concept = {it["id"]: it["concept_boost"] for it in out}
    centrality = {it["id"]: it["centrality_boost"] for it in out}
    jaccard = {it["id"]: it["jaccard_surprise"] for it in out}
    # Prove the factors actually differed (test is not vacuous):
    assert len({round(v, 6) for v in concept.values()}) > 1
    assert len({round(v, 6) for v in centrality.values()}) > 1
    assert len({round(v, 6) for v in jaccard.values()}) > 1
    # And yet the order is byte-for-byte the input order:
    assert [it["id"] for it in out] == [it["id"] for it in base]


# -- noisy runner: many random perturbations in a loop -----------------------


@pytest.mark.parametrize("trial", list(range(40)))
def test_noisy_runner_random_perturbations(trial: int, monkeypatch: pytest.MonkeyPatch) -> None:
    rnd = random.Random(trial)

    # Randomly vary which loaders are patched / which throw.
    if rnd.random() < 0.5:
        monkeypatch.setattr(
            E,
            "_load_centrality_map",
            lambda db_path: {i: rnd.random() for i in range(30)},
        )

    n = rnd.randint(1, 25)
    base = _make_items(n, seed=trial)
    # Drop / mangle random fields.
    for it in base:
        if rnd.random() < 0.3:
            it.pop("metadata", None)
        if rnd.random() < 0.2:
            it.pop("final_score", None)
    rnd.shuffle(base)

    out = E._apply_post_rank_metadata(base, f"q{trial}", db_path="/no/such/db")
    assert _ids(out) == _ids(base)
    # envelope keys always present
    assert all("concept_boost" in it for it in out)
    assert all("temporal_decay" in it for it in out)


# -- graceful degradation against a real (empty) temp DB ---------------------


def test_graceful_degradation_on_empty_db(tmp_path: Path) -> None:
    """Exercises the real DB-access paths (connection_pool.get + SELECT)
    against a temp empty sqlite. Missing tables must degrade to neutral
    envelope values without crashing or reordering."""
    db_path = tmp_path / "empty.db"
    items = _make_items(8, seed=7)
    out = E._apply_post_rank_metadata(items, "query", db_path=str(db_path))
    assert _ids(out) == _ids(items)
    for it in out:
        # concept/centrality/jaccard depend on DB tables that do not
        # exist in the empty temp DB, so they degrade to the neutral 1.0.
        assert it["concept_boost"] == 1.0
        assert it["centrality_boost"] == 1.0
        assert it["jaccard_surprise"] == 1.0
        # temporal_decay is computed from `created` (DB-independent),
        # so it is a real decayed float here, not the neutral 1.0.
        assert isinstance(it["temporal_decay"], float)


# -- static wiring: enrichment runs in exactly ONE place ----------------


def test_single_enrichment_site_wired() -> None:
    orch_path = Path(__file__).resolve().parent.parent / "search" / "orchestrator.py"
    orch_src = orch_path.read_text(encoding="utf-8")
    # The new single site is called inside _build_result_items.
    assert orch_src.count("_apply_post_rank_metadata(") == 1
    # The four legacy mutators must no longer steer ranking in orchestrator.
    assert "_apply_temporal_decay(out" not in orch_src
    assert "_apply_jaccard_surprise_penalty(out" not in orch_src
    assert "_apply_concept_boost(out" not in orch_src
    assert "_apply_centrality_boost(out" not in orch_src
    # The final ranking still sorts explicitly by the ranking score (r[6]).
    assert "out = sorted(" in orch_src
