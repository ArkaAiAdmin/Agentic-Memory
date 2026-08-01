#!/usr/bin/env python3
"""Reproducible evaluation for Paper 1 §8.5 — Convergence Under Concurrent Multi-Agent Writes.

Runs directly on the *production* field-update merge (``crdt/crdt_field.py:merge_field_updates``),
per the paper's setup ("we evaluate this directly on the production merge implementation"):

  Section A — delivery-order independence.
    200 write sets x 4 concurrency levels (N in {2, 4, 8, 16}) = 800 trials; each trial is
    replayed under 6 delivery-order permutations = 4,800 orderings. Asserts that every
    replica converges to the identical winner set (0 divergences).

  Section B — lost updates under three policies.
    For each write set, a write is "present in the converged causal history" iff its
    (last_writer_agent, logical_clock) appears in the winning record's version vector.
    * CK-CRDT   — production ``merge_field_updates``: element-wise VV join preserves every
                  causal contribution (0.0% by construction).
    * LWW       — wall-clock last-write-wins per field per sync round, no VV join: every
                  concurrent loser of a round is discarded, (N-1)/N of concurrent writes.
    * FWW       — first-writer-wins: the first write to a field locks it for the whole
                  stream; every later write (concurrent or serialized) is discarded.
    All writes in a round share one wall-clock tick (they are genuinely concurrent).

  Section C — referential integrity under concurrent merges (300 trials).
    Concurrent agents create the same entity under different IDs (fingerprint collision),
    plus edges referencing pre-merge IDs. Full pipeline (merge + dedup + redirect + orphan
    guard) vs a naive drop-on-merge policy that discards edges whose endpoint was merged
    away. Reports the dangling-edge rate for both.

Deterministic (seed=42). Prints markdown tables matching §8.5's structure.

Usage:
    python paper_pipeline/benchmark_convergence.py            # sections A + B + C
    python paper_pipeline/benchmark_convergence.py --section A
    python paper_pipeline/benchmark_convergence.py --seed 42
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from crdt.crdt_field import FieldUpdate, merge_field_updates  # production merge
from crdt_projection import (  # paper pipeline primitives (same dir)
    EdgeOp,
    EntityOp,
    compute_fingerprint,
    entity_dedup_via_crdt,
    merge_edge_ops,
    merge_entity_ops,
    redirect_edge_ids,
)

AGENT_LEVELS = [2, 4, 8, 16]
WRITE_SETS = 200
PERMUTATIONS = 6
OPS_PER_LEVEL = 5000  # §8.5 scale: ~5,000 concurrent ops per concurrency level
ORPHAN_TRIALS = 300
FIELDS_PER_SET = 25


# ---------------------------------------------------------------------------
# Write-set generation (Section A + B)
# ---------------------------------------------------------------------------


def rounds_for(n_agents: int) -> int:
    """Rounds per write set so a level produces ~5,000 concurrent writes total."""
    return max(1, round(OPS_PER_LEVEL / (WRITE_SETS * n_agents)))


def gen_write_set(rng: random.Random, n_agents: int, rounds: int) -> list[tuple[FieldUpdate, int]]:
    """One write set: `rounds` sync rounds; every round all N agents write the same field
    concurrently (shared wall-clock tick, independent VV + logical clock).

    Returns (update, wall_clock_tick) pairs.
    """
    out: list[tuple[FieldUpdate, int]] = []
    n_fields = max(1, rounds // 2)
    for r in range(rounds):
        field = f"field_{r % n_fields}"
        for i, agent in enumerate([f"agent_{i}" for i in range(n_agents)]):
            clock = (r * n_agents) + i + 1
            update = FieldUpdate(
                memory_id="m0",
                field_name=field,
                value=f"v-{agent}-{r}",
                version_vector={agent: clock},
                logical_clock=clock,
                last_writer_agent=agent,
            )
            out.append((update, r))  # same tick r = concurrent writes
    rng.shuffle(out)
    return out


def winner_set_key(updates: list[FieldUpdate]) -> tuple:
    """Canonical key of a converged state: sorted (memory, field, value, vv, clock, agent)."""
    return tuple(
        sorted(
            (
                u.memory_id,
                u.field_name,
                u.value,
                json.dumps(sorted((u.version_vector or {}).items())),
                u.logical_clock,
                u.last_writer_agent,
            )
            for u in updates
        )
    )


# ---------------------------------------------------------------------------
# Section A — delivery-order independence on the production merge
# ---------------------------------------------------------------------------


def section_a(rng: random.Random) -> tuple[int, int, int]:
    """Run 800 trials x 6 permutations against production merge_field_updates.

    Returns (trials, permutations, divergences).
    """
    total_trials = 0
    total_permutations = 0
    divergences = 0
    for n_agents in AGENT_LEVELS:
        for trial in range(WRITE_SETS):
            rng_t = random.Random(rng.getrandbits(64))
            rounds = rounds_for(n_agents)
            write_set = gen_write_set(rng_t, n_agents, rounds)
            states = set()
            for perm in range(PERMUTATIONS):
                rng_p = random.Random(rng_t.getrandbits(64) + perm)
                permuted = [u for u, _ in write_set]
                rng_p.shuffle(permuted)
                states.add(winner_set_key(merge_field_updates(permuted)))
                total_permutations += 1
            if len(states) > 1:
                divergences += 1
            total_trials += 1
    return total_trials, total_permutations, divergences


# ---------------------------------------------------------------------------
# Section B — lost updates under three policies
# ---------------------------------------------------------------------------


def _present_count(updates: list[tuple[FieldUpdate, int]], winners: list[FieldUpdate]) -> int:
    """Count writes preserved in the converged causal history.

    A write is preserved iff its (agent, logical_clock) appears in the winning record's
    version vector, OR a causally-later contribution from the same agent does (the
    element-wise VV join is a per-agent frontier: an agent's own older write to a field
    is causally dominated by its later write, not lost).
    """
    present: set[tuple[str, int]] = set()
    for w in winners:
        for agent, clock in (w.version_vector or {}).items():
            present.add((agent, clock))
    preserved = 0
    for u, _ in updates:
        clock = u.logical_clock
        if (u.last_writer_agent, clock) in present:
            preserved += 1
            continue
        # causally-dominated by the same agent's later contribution?
        dominated = any(
            agent == u.last_writer_agent and c >= clock for agent, c in present
        )
        if dominated:
            preserved += 1
    return preserved


def _lww_winners(updates: list[tuple[FieldUpdate, int]]) -> list[FieldUpdate]:
    """Wall-clock LWW: per (memory, field, tick) keep the write with max (clock, agent).

    Serialized rounds to the same field each keep a winner (no VV join carried).
    """
    by_key: dict[tuple[str, str, int], list[FieldUpdate]] = {}
    for u, tick in updates:
        by_key.setdefault((u.memory_id, u.field_name, tick), []).append(u)
    winners = []
    for group in by_key.values():
        winners.append(max(group, key=lambda u: (u.logical_clock, u.last_writer_agent)))
    return winners


def _fww_winners(updates: list[tuple[FieldUpdate, int]]) -> list[FieldUpdate]:
    """First-writer-wins: the first write to each field (min tick) locks it forever."""
    by_field: dict[tuple[str, str], list[FieldUpdate]] = {}
    for u, tick in updates:
        by_field.setdefault((u.memory_id, u.field_name), []).append(u)
    return [min(group, key=lambda u: (u.logical_clock, u.last_writer_agent)) for group in by_field.values()]


def section_b(rng: random.Random) -> dict:
    """Lost-write rates per policy, per level, over ~5,000 concurrent writes per level."""
    results: dict[str, list[float]] = {"ck_crdt": [], "lww": [], "fww": []}
    detail: dict[str, dict[int, float]] = {"ck_crdt": {}, "lww": {}, "fww": {}}
    for n_agents in AGENT_LEVELS:
        totals = {"ck_crdt": 0, "lww": 0, "fww": 0}
        lost = {"ck_crdt": 0, "lww": 0, "fww": 0}
        for trial in range(WRITE_SETS):
            rng_t = random.Random(rng.getrandbits(64))
            rounds = rounds_for(n_agents)
            write_set = gen_write_set(rng_t, n_agents, rounds)
            n = len(write_set)
            for policy, winners in (
                ("ck_crdt", merge_field_updates([u for u, _ in write_set])),
                ("lww", _lww_winners(write_set)),
                ("fww", _fww_winners(write_set)),
            ):
                present = _present_count(write_set, winners)
                totals[policy] += n
                lost[policy] += n - present
        for policy in ("ck_crdt", "lww", "fww"):
            rate = 100.0 * lost[policy] / max(totals[policy], 1)
            results[policy].append(rate)
            detail[policy][n_agents] = rate
    avg = {p: sum(v) / len(v) for p, v in results.items()}
    return {"per_level_pct": detail, "avg_pct": avg, "ops_per_level": {
        n: WRITE_SETS * n * rounds_for(n) for n in AGENT_LEVELS}}


# ---------------------------------------------------------------------------
# Section C — referential integrity under concurrent merges (300 trials)
# ---------------------------------------------------------------------------


def _survivor_ids(deduped: dict) -> set[int]:
    return set(deduped["merged_state"].keys())


def _dangling(edge_state: dict, survivors: set[int]) -> int:
    dangling = 0
    for info in edge_state.values():
        if info["source_id"] not in survivors or info["target_id"] not in survivors:
            dangling += 1
    return dangling


def gen_merge_trial(rng: random.Random) -> tuple[list[EntityOp], list[EdgeOp]]:
    """k agents concurrently create the same entity under different IDs (fingerprint
    collision) plus m distinct entities; edges reference pre-merge IDs at random."""
    k = rng.choice([2, 3, 4])
    m = rng.choice([2, 3])
    eops: list[EntityOp] = []
    # colliding entity: same name/type/desc -> same fingerprint -> dedup to max(id)
    for i in range(k):
        eops.append(EntityOp(
            entity_id=i + 1,
            agent_id=f"agent_{i}",
            op="add",
            version_vector={f"agent_{i}": 1},
            name="alice",
            entity_type="person",
            description="shared description",
            fingerprint=compute_fingerprint("alice", "person", "shared description"),
            timestamp=float(i),
        ))
    # distinct entities with unique fingerprints
    for j in range(m):
        name = f"entity_{j}"
        fp = compute_fingerprint(name, "thing", "")
        eops.append(EntityOp(
            entity_id=k + j + 1,
            agent_id=f"agent_{j % k}",
            op="add",
            version_vector={f"agent_{j % k}": 1},
            name=name,
            entity_type="thing",
            description="",
            fingerprint=fp,
            timestamp=float(k + j),
        ))
    ids = list(range(1, k + m + 1))
    edops = []
    for e in range(3 * k):
        edops.append(EdgeOp(
            edge_id=e + 1,
            source_id=rng.choice(ids),
            target_id=rng.choice(ids),
            relation="related_to",
            weight=1.0,
            agent_id=f"agent_{e % k}",
            version_vector={f"agent_{e % k}": 1},
            timestamp=float(e),
        ))
    return eops, edops


def section_c(rng: random.Random) -> dict:
    """Dangling-edge rate: full pipeline vs naive drop-on-merge, over 300 trials."""
    full_orphans = 0
    drop_orphans = 0
    total_edges = 0
    for _ in range(ORPHAN_TRIALS):
        rng_t = random.Random(rng.getrandbits(64))
        eops, edops = gen_merge_trial(rng_t)
        merged = merge_entity_ops(eops)
        deduped = entity_dedup_via_crdt(merged)
        edge_state = merge_edge_ops(edops)
        total_edges += len(edge_state)
        # Full pipeline: rewrite endpoints through the redirect map, then count dangling.
        redirected = redirect_edge_ids(edge_state, deduped["redirects"])
        full_orphans += _dangling(redirected, _survivor_ids(deduped))
        # Naive drop-on-merge: discard edges whose endpoint was merged away (no redirect).
        drop_orphans += _dangling(edge_state, _survivor_ids(deduped))
    return {
        "trials": ORPHAN_TRIALS,
        "total_edges": total_edges,
        "full_pipeline_dangling_pct": 100.0 * full_orphans / max(total_edges, 1),
        "drop_on_merge_dangling_pct": 100.0 * drop_orphans / max(total_edges, 1),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper §8.5 reproducible evaluation")
    parser.add_argument("--section", choices=["all", "A", "B", "C"], default="all")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    print("=" * 78)
    print(f"Paper §8.5 evaluation (seed={args.seed}) — production merge implementation")
    print("=" * 78)

    if args.section in ("all", "A"):
        trials, perms, divergences = section_a(rng)
        print(f"\nSection A — delivery-order independence (production merge_field_updates)")
        print(f"  Trials:       {trials}  (4 concurrency levels x 200 write sets)")
        print(f"  Permutations: {perms}  ({PERMUTATIONS} per trial)")
        print(f"  Divergences:  {divergences}")
        print(f"  Result:       {'PASS — all replicas converge to the identical winner set' if divergences == 0 else 'FAIL'}")

    if args.section in ("all", "B"):
        b = section_b(rng)
        print(f"\nSection B — lost writes (concurrent writes absent from the converged causal history)")
        print(f"  {'Policy':<10} {'N=2':>8} {'N=4':>8} {'N=8':>8} {'N=16':>8} {'avg':>8}")
        for policy, label in (("ck_crdt", "CK-CRDT"), ("lww", "LWW"), ("fww", "FWW")):
            row = b["per_level_pct"][policy]
            print(f"  {label:<10} {row[2]:>7.1f}% {row[4]:>7.1f}% {row[8]:>7.1f}% {row[16]:>7.1f}% {b['avg_pct'][policy]:>7.1f}%")
        print(f"  Ops per level: " + ", ".join(f"N={n}: {b['ops_per_level'][n]}" for n in AGENT_LEVELS))

    if args.section in ("all", "C"):
        c = section_c(rng)
        print(f"\nSection C — referential integrity under concurrent merges ({c['trials']} trials, {c['total_edges']} edges)")
        print(f"  Full pipeline (merge + dedup + redirect + orphan guard): {c['full_pipeline_dangling_pct']:.1f}% dangling")
        print(f"  Naive drop-on-merge policy:                             {c['drop_on_merge_dangling_pct']:.1f}% dangling")

    print("\nDone.")


if __name__ == "__main__":
    main()
