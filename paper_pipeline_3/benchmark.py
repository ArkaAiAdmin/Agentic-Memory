"""
Systems-paper evaluation for the CK-CRDT knowledge-graph projection pipeline.

Extends the baseline comparison from paper_pipeline/benchmark.py with:
1. Realistic multi-agent workload generator (Zipf entity distribution, bursty writes)
2. Multi-agent convergence evaluation (delivery-order independence)
3. Comparison against centralized-coordinator baseline
4. Memory usage analysis
5. Latency percentile analysis (p50, p95, p99)
6. Concurrent writer scaling (2, 4, 8, 16, 32 agents)
"""

import hashlib
import json
import random
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Data structures (from crdt_projection.py)
# ---------------------------------------------------------------------------


@dataclass
class EntityOp:
    entity_id: int
    agent_id: str
    op: str
    version_vector: Dict[str, int] = field(default_factory=dict)
    name: str = ""
    entity_type: str = ""
    description: str = ""
    fingerprint: str = ""
    timestamp: float = 0.0


@dataclass
class EdgeOp:
    edge_id: int
    source_id: int
    target_id: int
    relation: str = "related_to"
    weight: float = 1.0
    valid_at: Optional[str] = None
    agent_id: str = ""
    version_vector: Dict[str, int] = field(default_factory=dict)
    timestamp: float = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def vv_dominates(a: Dict[str, int], b: Dict[str, int]) -> bool:
    if not a or not b:
        return False
    all_peers = set(a) | set(b)
    return all(a.get(p, 0) >= b.get(p, 0) for p in all_peers) and any(a.get(p, 0) > b.get(p, 0) for p in all_peers)


def _serialise_vv(v: Dict[str, int]) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"))


def compute_fingerprint(name: str, entity_type: str, description: str = "") -> str:
    canonical = lambda s: " ".join(s.lower().strip().split())
    payload = f"{canonical(name)}|{canonical(entity_type)}|{canonical(description)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Pipeline phases
# ---------------------------------------------------------------------------


def merge_entity_ops(ops: List[EntityOp]) -> Dict[int, Dict[str, Any]]:
    by_entity: Dict[int, List[EntityOp]] = {}
    for op in ops:
        by_entity.setdefault(op.entity_id, []).append(op)

    result: Dict[int, Dict[str, Any]] = {}
    for entity_id, ops_for_entity in by_entity.items():
        sorted_ops = sorted(ops_for_entity, key=lambda o: (o.timestamp, _serialise_vv(o.version_vector)))
        adds = [o for o in sorted_ops if o.op == "add"]
        removes = [o for o in sorted_ops if o.op == "remove"]
        if not adds:
            continue
        is_tombstoned = any(vv_dominates(r.version_vector, a.version_vector) for a in adds for r in removes)
        if is_tombstoned:
            continue

        def _winner(field_name: str) -> str:
            candidates = [o for o in adds if getattr(o, field_name, "")]
            if not candidates:
                return ""
            w = candidates[0]
            for c in candidates[1:]:
                if vv_dominates(c.version_vector, w.version_vector):
                    w = c
                elif not vv_dominates(w.version_vector, c.version_vector):
                    if c.timestamp > w.timestamp or (c.timestamp == w.timestamp and c.agent_id < w.agent_id):
                        w = c
            return str(getattr(w, field_name))

        fp = next((a.fingerprint for a in adds if a.fingerprint), "")
        result[entity_id] = {
            "tombstone": False, "name": _winner("name"), "entity_type": _winner("entity_type"),
            "description": _winner("description"), "fingerprint": fp,
        }
    return result


def entity_dedup(state: Dict[int, Dict[str, Any]]) -> Tuple[Dict[int, Dict[str, Any]], Dict[int, int]]:
    by_fp: Dict[str, List[int]] = {}
    for eid, info in state.items():
        if info.get("tombstone"):
            continue
        fp = info.get("fingerprint", "")
        if not fp:
            fp = compute_fingerprint(info.get("name", ""), info.get("entity_type", ""), info.get("description", ""))
            info["fingerprint"] = fp
        by_fp.setdefault(fp, []).append(eid)

    deduped: Dict[int, Dict[str, Any]] = {}
    redirects: Dict[int, int] = {}
    for _fp, ids in by_fp.items():
        if len(ids) == 1:
            deduped[ids[0]] = state[ids[0]]
            continue
        winner = max(ids)
        deduped[winner] = state[winner]
        for loser in ids:
            if loser != winner:
                redirects[loser] = winner
    return deduped, redirects


def merge_edges(ops: List[EdgeOp]) -> Dict[int, Dict[str, Any]]:
    by_edge: Dict[int, List[EdgeOp]] = {}
    for op in ops:
        by_edge.setdefault(op.edge_id, []).append(op)
    result: Dict[int, Dict[str, Any]] = {}
    for eid, eops in by_edge.items():
        w = eops[0]
        for c in eops[1:]:
            if vv_dominates(c.version_vector, w.version_vector):
                w = c
            elif not vv_dominates(w.version_vector, c.version_vector):
                if c.timestamp > w.timestamp or (c.timestamp == w.timestamp and c.agent_id < w.agent_id):
                    w = c
        result[eid] = {"source_id": w.source_id, "target_id": w.target_id, "relation": w.relation, "weight": w.weight}
    return result


def redirect_edges(edges: Dict[int, Dict[str, Any]], redirects: Dict[int, int]) -> Dict[int, Dict[str, Any]]:
    if not redirects:
        return edges
    return {
        eid: {**info, "source_id": redirects.get(info["source_id"], info["source_id"]),
              "target_id": redirects.get(info["target_id"], info["target_id"])}
        for eid, info in edges.items()
    }


# ---------------------------------------------------------------------------
# Approaches
# ---------------------------------------------------------------------------


def full_pipeline(eops: List[EntityOp], edops: List[EdgeOp]) -> Dict:
    """Full CK-CRDT pipeline: merge + dedup + redirect + orphan guard."""
    merged = merge_entity_ops(eops)
    canonical, redirects = entity_dedup(merged)
    edges = merge_edges(edops)
    edges = redirect_edges(edges, redirects)
    canonical_ids = set(canonical.keys())
    edges = {eid: info for eid, info in edges.items()
             if info["source_id"] in canonical_ids and info["target_id"] in canonical_ids}
    return {"entities": canonical, "edges": edges, "redirects": redirects}


def naive_merge(eops: List[EntityOp], edops: List[EdgeOp]) -> Dict:
    """Baseline: no dedup, no redirect (equivalent to Yjs/Automerge semantics)."""
    merged = merge_entity_ops(eops)
    edges = merge_edges(edops)
    return {"entities": merged, "edges": edges, "redirects": {}}


def centralized_coordinator(eops: List[EntityOp], edops: List[EdgeOp]) -> Dict:
    """Simulates a centralized write coordinator (what Zep/Mem0 do).

    All operations are serialized through a single writer. Concurrent writes
    are resolved by wall-clock timestamp (LWW). This is the baseline that
    production agent-memory systems use.
    """
    # Sort all entity ops by timestamp (simulating serialized write order)
    sorted_eops = sorted(eops, key=lambda o: o.timestamp)
    # Apply LWW: for each entity_id, keep only the latest write
    latest_per_entity: Dict[int, EntityOp] = {}
    for op in sorted_eops:
        if op.entity_id not in latest_per_entity or op.timestamp > latest_per_entity[op.entity_id].timestamp:
            latest_per_entity[op.entity_id] = op

    # Build result from LWW winners only
    result_entities: Dict[int, Dict[str, Any]] = {}
    for eid, op in latest_per_entity.items():
        if op.op == "add":
            fp = op.fingerprint or compute_fingerprint(op.name, op.entity_type, op.description)
            result_entities[eid] = {
                "tombstone": False, "name": op.name, "entity_type": op.entity_type,
                "description": op.description, "fingerprint": fp,
            }

    # Edges: keep all (no orphan guard in centralized model)
    edges = merge_edges(edops)
    return {"entities": result_entities, "edges": edges, "redirects": {}}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def count_orphans(result: Dict) -> int:
    entity_ids = set(result["entities"].keys())
    return sum(1 for info in result["edges"].values()
               if info["source_id"] not in entity_ids or info["target_id"] not in entity_ids)


def count_duplicates(result: Dict) -> int:
    fp_map: Dict[str, List[int]] = {}
    for eid, info in result["entities"].items():
        fp = info.get("fingerprint", "") or compute_fingerprint(info["name"], info["entity_type"], info.get("description", ""))
        fp_map.setdefault(fp, []).append(eid)
    return sum(len(ids) - 1 for ids in fp_map.values() if len(ids) > 1)


def count_lost_writes(result: Dict, total_ops: int) -> int:
    """Count operations not reflected in the converged state."""
    return total_ops - len(result["entities"])


# ---------------------------------------------------------------------------
# Realistic workload generator
# ---------------------------------------------------------------------------


def generate_realistic_workload(
    n_ops: int = 100_000,
    n_agents: int = 16,
    n_distinct_entities: int = 1000,
    zipf_exponent: float = 1.0,
    collision_rate: float = 0.3,
    edge_ratio: float = 0.2,
    seed: int = 42,
) -> Tuple[List[EntityOp], List[EdgeOp]]:
    """Generate a realistic multi-agent workload.

    Models real agent behavior:
    - Zipf distribution for entity popularity (few entities are very popular)
    - Bursty writes (not uniform)
    - Multiple agents creating the same entity independently (collisions)
    - Edges between entities

    Args:
        n_ops: Total number of entity operations.
        n_agents: Number of concurrent agents.
        n_distinct_entities: Number of distinct real-world entities.
        zipf_exponent: Zipf distribution exponent (higher = more skewed).
        collision_rate: Fraction of ops that are collisions (same entity, different agent).
        edge_ratio: Fraction of ops that become edges (vs. entity-only).
        seed: Random seed for reproducibility.

    Returns:
        (entity_ops, edge_ops)
    """
    rng = random.Random(seed)

    # Zipf weights for entity popularity
    weights = [1.0 / (i + 1) ** zipf_exponent for i in range(n_distinct_entities)]
    total_weight = sum(weights)
    weights = [w / total_weight for w in weights]

    # Entity templates (name, type, description)
    entity_templates = []
    for i in range(n_distinct_entities):
        entity_templates.append((
            f"entity_{i}",
            rng.choice(["person", "project", "concept", "organization", "location"]),
            f"description_{i % 50}",  # Some descriptions repeat (causing collisions)
        ))

    eops: List[EntityOp] = []
    edops: List[EdgeOp] = []
    entity_id_counter = 0
    edge_id_counter = 0

    # Track which (entity_template, agent) pairs have been created
    created: Dict[Tuple[int, str], int] = {}  # (template_idx, agent_id) -> entity_id

    for op_idx in range(n_ops):
        agent_id = f"agent_{op_idx % n_agents}"
        timestamp = float(op_idx)

        # Select entity template by Zipf distribution
        r = rng.random()
        cumulative = 0.0
        template_idx = 0
        for i, w in enumerate(weights):
            cumulative += w
            if r <= cumulative:
                template_idx = i
                break

        name, etype, desc = entity_templates[template_idx]

        # Decide if this is a collision (same entity, different agent)
        is_collision = rng.random() < collision_rate and (template_idx, agent_id) not in created

        if is_collision:
            # Create a new entity_id for the same template (collision)
            entity_id_counter += 1
            eid = entity_id_counter
        elif (template_idx, agent_id) in created:
            # Update existing entity
            eid = created[(template_idx, agent_id)]
        else:
            # New entity creation
            entity_id_counter += 1
            eid = entity_id_counter
            created[(template_idx, agent_id)] = eid

        vv = {agent_id: op_idx // n_agents + 1}
        fp = compute_fingerprint(name, etype, desc)

        eops.append(EntityOp(
            entity_id=eid,
            agent_id=agent_id,
            op="add",
            version_vector=vv,
            name=name,
            entity_type=etype,
            description=desc,
            fingerprint=fp,
            timestamp=timestamp,
        ))

        # Generate edges
        if rng.random() < edge_ratio and len(created) > 1:
            # Pick a random target entity
            target_template = rng.randint(0, n_distinct_entities - 1)
            target_agents = [a for (t, a), eid in created.items() if t == target_template]
            if target_agents:
                target_agent = rng.choice(target_agents)
                target_eid = created[(target_template, target_agent)]
                if target_eid != eid:
                    edge_id_counter += 1
                    edops.append(EdgeOp(
                        edge_id=edge_id_counter,
                        source_id=eid,
                        target_id=target_eid,
                        relation=rng.choice(["related_to", "knows", "works_with", "depends_on"]),
                        weight=rng.uniform(0.1, 1.0),
                        agent_id=agent_id,
                        version_vector=vv,
                        timestamp=timestamp,
                    ))

    return eops, edops


# ---------------------------------------------------------------------------
# Multi-agent convergence evaluation
# ---------------------------------------------------------------------------


def evaluate_convergence(n_trials: int = 200, n_agents_list: Optional[List[int]] = None) -> Dict:
    """Evaluate delivery-order independence (convergence) under concurrent multi-agent writes.

    For each trial, generate a random write set, then evaluate all permutations
    of delivery order. Measure whether all permutations produce the same canonical state.

    Returns:
        {
            "n_trials": int,
            "n_divergences": int,
            "lost_writes_pct": float,
            "orphan_edges": int,
        }
    """
    if n_agents_list is None:
        n_agents_list = [2, 4, 8, 16]

    total_divergences = 0
    total_lost_writes = 0
    total_orphans = 0
    total_trials = 0

    for n_agents in n_agents_list:
        for trial in range(n_trials):
            # Generate a small write set for this trial
            eops, edops = generate_realistic_workload(
                n_ops=n_agents * 20,  # 20 ops per agent
                n_agents=n_agents,
                n_distinct_entities=n_agents * 3,
                collision_rate=0.5,  # High collision rate to stress convergence
                seed=trial * 1000 + n_agents,
            )

            # Evaluate multiple delivery orders
            results = []
            for perm_idx in range(6):  # 6 random permutations
                rng = random.Random(perm_idx * 100 + trial)
                permuted_eops = list(eops)
                rng.shuffle(permuted_eops)
                result = full_pipeline(permuted_eops, edops)
                # Canonical state as hashable key
                entity_key = tuple(sorted(result["entities"].keys()))
                edge_key = tuple(sorted(
                    (info["source_id"], info["target_id"], info["relation"])
                    for info in result["edges"].values()
                ))
                results.append((entity_key, edge_key))

            # Check for divergence
            if len(set(results)) > 1:
                total_divergences += 1

            # Measure lost writes and orphans on the last result
            total_lost_writes += count_lost_writes(result, len(eops))
            total_orphans += count_orphans(result)
            total_trials += 1

    return {
        "n_trials": total_trials,
        "n_divergences": total_divergences,
        "lost_writes_pct": total_lost_writes / max(total_trials * 1, 1) * 100,
        "orphan_edges": total_orphans,
    }


# ---------------------------------------------------------------------------
# Latency percentile analysis
# ---------------------------------------------------------------------------


def measure_latency_percentiles(
    n_ops: int = 100_000,
    n_agents: int = 16,
    rounds: int = 20,
) -> Dict:
    """Measure latency percentiles (p50, p95, p99) for the full pipeline.

    Returns:
        {
            "p50_us": float,
            "p95_us": float,
            "p99_us": float,
            "mean_us": float,
            "throughput_ops_s": float,
        }
    """
    eops, edops = generate_realistic_workload(
        n_ops=n_ops, n_agents=n_agents, n_distinct_entities=1000,
    )

    # Warmup
    for _ in range(3):
        full_pipeline(eops, edops)

    latencies = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        full_pipeline(eops, edops)
        t1 = time.perf_counter()
        latencies.append((t1 - t0) * 1_000_000)  # microseconds

    latencies.sort()
    n = len(latencies)

    return {
        "p50_us": latencies[n // 2],
        "p95_us": latencies[int(n * 0.95)],
        "p99_us": latencies[int(n * 0.99)],
        "mean_us": statistics.mean(latencies),
        "throughput_ops_s": n_ops / (statistics.mean(latencies) / 1_000_000),
    }


# ---------------------------------------------------------------------------
# Memory usage analysis
# ---------------------------------------------------------------------------


def measure_memory_usage(n_ops_list: Optional[List[int]] = None) -> Dict:
    """Measure memory usage at different scales.

    Returns:
        {
            "100K": {"entities": int, "redirects": int, "edges": int, "est_mb": float},
            "1M": {...},
            "10M": {...},
        }
    """
    if n_ops_list is None:
        n_ops_list = [100_000, 1_000_000]

    results = {}
    for n_ops in n_ops_list:
        eops, edops = generate_realistic_workload(n_ops=n_ops, n_agents=16, n_distinct_entities=1000)

        result = full_pipeline(eops, edops)

        # Estimate memory usage (rough: count objects and their sizes)
        n_entities = len(result["entities"])
        n_redirects = len(result["redirects"])
        n_edges = len(result["edges"])

        # Rough estimate: each entity ~200 bytes, each redirect ~50 bytes, each edge ~100 bytes
        est_bytes = n_entities * 200 + n_redirects * 50 + n_edges * 100
        est_mb = est_bytes / (1024 * 1024)

        label = f"{n_ops // 1000}K" if n_ops < 1_000_000 else f"{n_ops // 1_000_000}M"
        results[label] = {
            "entities": n_entities,
            "redirects": n_redirects,
            "edges": n_edges,
            "est_mb": round(est_mb, 2),
        }

    return results


# ---------------------------------------------------------------------------
# Concurrent writer scaling
# ---------------------------------------------------------------------------


def measure_concurrent_writer_scaling(
    n_ops_per_agent: int = 5000,
    n_agents_list: Optional[List[int]] = None,
) -> Dict:
    """Measure throughput as the number of concurrent writers increases.

    Returns:
        {
            "2_agents": {"throughput_ops_s": float, "time_s": float},
            "4_agents": {...},
            ...
        }
    """
    if n_agents_list is None:
        n_agents_list = [2, 4, 8, 16, 32]

    results = {}
    for n_agents in n_agents_list:
        n_ops = n_ops_per_agent * n_agents
        eops, edops = generate_realistic_workload(
            n_ops=n_ops, n_agents=n_agents, n_distinct_entities=500,
        )

        # Warmup
        for _ in range(3):
            full_pipeline(eops, edops)

        # Timed runs
        rounds = 10
        t0 = time.perf_counter()
        for _ in range(rounds):
            full_pipeline(eops, edops)
        t1 = time.perf_counter()

        time_s = (t1 - t0) / rounds
        throughput = n_ops / time_s

        results[f"{n_agents}_agents"] = {
            "n_ops": n_ops,
            "time_s": round(time_s, 3),
            "throughput_ops_s": round(throughput, 0),
        }

    return results


# ---------------------------------------------------------------------------
# Baseline comparison (with realistic workload)
# ---------------------------------------------------------------------------


def compare_baselines(n_ops: int = 100_000, n_agents: int = 16) -> Dict:
    """Compare full pipeline against baselines on a realistic workload.

    Baselines:
    1. Naive merge (no dedup, no redirect) — equivalent to Yjs/Automerge semantics
    2. Centralized coordinator (LWW) — what Zep/Mem0 do

    Returns:
        {
            "naive": {"entities": int, "duplicates": int, "orphans": int, "lost_writes": int, "time_us": float},
            "centralized": {...},
            "full_pipeline": {...},
        }
    """
    eops, edops = generate_realistic_workload(n_ops=n_ops, n_agents=n_agents, n_distinct_entities=1000)

    results = {}
    for name, fn in [("naive", naive_merge), ("centralized", centralized_coordinator), ("full_pipeline", full_pipeline)]:
        # Warmup
        for _ in range(3):
            fn(eops, edops)

        # Timed runs
        rounds = 10
        t0 = time.perf_counter()
        for _ in range(rounds):
            r = fn(eops, edops)
        t1 = time.perf_counter()

        results[name] = {
            "entities": len(r["entities"]),
            "edges": len(r["edges"]),
            "duplicates": count_duplicates(r),
            "orphans": count_orphans(r),
            "lost_writes": count_lost_writes(r, len(eops)),
            "redirects": len(r["redirects"]),
            "time_us": (t1 - t0) / rounds * 1_000_000,
        }

    return results


# ---------------------------------------------------------------------------
# Ablation study
# ---------------------------------------------------------------------------


def run_ablation(n_ops: int = 100_000, n_agents: int = 16) -> Dict:
    """Ablation study: measure the contribution of each pipeline phase.

    Configurations:
    1. Phase 1 only (merge) — no dedup, no edge redirect
    2. Phase 1+2 (merge+dedup) — dedup but no edge redirect/orphan guard
    3. Full pipeline (all 3 phases) — merge + dedup + redirect + orphan guard

    Returns:
        {
            "phase1": {"entities": int, "duplicates": int, "orphans": int, "redirects": int, "time_ms": float},
            "phase12": {...},
            "full_pipeline": {...},
        }
    """
    eops, edops = generate_realistic_workload(n_ops=n_ops, n_agents=n_agents, n_distinct_entities=1000)

    results = {}

    # Phase 1 only: merge_entity_ops (no dedup, no edge processing)
    for _ in range(3):
        merged = merge_entity_ops(eops)
    rounds = 10
    t0 = time.perf_counter()
    for _ in range(rounds):
        merged = merge_entity_ops(eops)
    t1 = time.perf_counter()
    phase1_time_ms = (t1 - t0) / rounds * 1000

    # Count duplicates in phase1 result
    phase1_dupes = count_duplicates({"entities": merged})
    # Count edges that would be orphans (no redirect, no orphan guard)
    raw_edges = merge_edges(edops)
    phase1_entity_ids = set(merged.keys())
    phase1_orphans = sum(1 for info in raw_edges.values()
                         if info["source_id"] not in phase1_entity_ids or info["target_id"] not in phase1_entity_ids)

    results["phase1"] = {
        "entities": len(merged),
        "duplicates": phase1_dupes,
        "orphans": phase1_orphans,
        "redirects": 0,
        "time_ms": round(phase1_time_ms, 1),
    }

    # Phase 1+2: merge + entity dedup (no edge redirect)
    for _ in range(3):
        merged2 = merge_entity_ops(eops)
        canonical2, redirects2 = entity_dedup(merged2)
    t0 = time.perf_counter()
    for _ in range(rounds):
        merged2 = merge_entity_ops(eops)
        canonical2, redirects2 = entity_dedup(merged2)
    t1 = time.perf_counter()
    phase12_time_ms = (t1 - t0) / rounds * 1000

    phase12_dupes = count_duplicates({"entities": canonical2})
    # Edges still not redirected — count orphans against canonical set
    phase12_entity_ids = set(canonical2.keys())
    phase12_orphans = sum(1 for info in raw_edges.values()
                          if info["source_id"] not in phase12_entity_ids or info["target_id"] not in phase12_entity_ids)

    results["phase12"] = {
        "entities": len(canonical2),
        "duplicates": phase12_dupes,
        "orphans": phase12_orphans,
        "redirects": len(redirects2),
        "time_ms": round(phase12_time_ms, 1),
    }

    # Full pipeline: all 3 phases
    for _ in range(3):
        full_result = full_pipeline(eops, edops)
    t0 = time.perf_counter()
    for _ in range(rounds):
        full_result = full_pipeline(eops, edops)
    t1 = time.perf_counter()
    full_time_ms = (t1 - t0) / rounds * 1000

    results["full_pipeline"] = {
        "entities": len(full_result["entities"]),
        "duplicates": count_duplicates(full_result),
        "orphans": count_orphans(full_result),
        "redirects": len(full_result["redirects"]),
        "time_ms": round(full_time_ms, 1),
    }

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("=" * 100)
    print("CK-CRDT Knowledge-Graph Projection: Systems-Paper Evaluation")
    print("=" * 100)

    # 1. Baseline comparison
    print("\n--- Baseline Comparison (100K ops, 16 agents) ---")
    baselines = compare_baselines(n_ops=100_000, n_agents=16)
    print(f"{'Approach':<20} {'Entities':>10} {'Dupes':>8} {'Orphans':>8} {'Lost':>8} {'Time(us)':>12}")
    print("-" * 70)
    for name, metrics in baselines.items():
        print(f"{name:<20} {metrics['entities']:>10} {metrics['duplicates']:>8} "
              f"{metrics['orphans']:>8} {metrics['lost_writes']:>8} {metrics['time_us']:>12.0f}")

    # 2. Convergence evaluation
    print("\n--- Convergence Evaluation (delivery-order independence) ---")
    convergence = evaluate_convergence(n_trials=200, n_agents_list=[2, 4, 8, 16])
    print(f"  Trials: {convergence['n_trials']}")
    print(f"  Divergences: {convergence['n_divergences']}")
    print(f"  Lost writes: {convergence['lost_writes_pct']:.1f}%")
    print(f"  Orphan edges: {convergence['orphan_edges']}")

    # 3. Latency percentiles
    print("\n--- Latency Percentiles (100K ops, 16 agents) ---")
    latency = measure_latency_percentiles(n_ops=100_000, n_agents=16)
    print(f"  p50: {latency['p50_us']:.0f} us")
    print(f"  p95: {latency['p95_us']:.0f} us")
    print(f"  p99: {latency['p99_us']:.0f} us")
    print(f"  Mean: {latency['mean_us']:.0f} us")
    print(f"  Throughput: {latency['throughput_ops_s']:.0f} ops/s")

    # 4. Memory usage
    print("\n--- Memory Usage ---")
    memory = measure_memory_usage(n_ops_list=[100_000, 1_000_000])
    print(f"{'Scale':<10} {'Entities':>10} {'Redirects':>10} {'Edges':>10} {'Est. MB':>10}")
    print("-" * 55)
    for label, metrics in memory.items():
        print(f"{label:<10} {metrics['entities']:>10} {metrics['redirects']:>10} "
              f"{metrics['edges']:>10} {metrics['est_mb']:>10.2f}")

    # 5. Concurrent writer scaling
    print("\n--- Concurrent Writer Scaling (5K ops/agent) ---")
    scaling = measure_concurrent_writer_scaling(n_ops_per_agent=5000, n_agents_list=[2, 4, 8, 16, 32])
    print(f"{'Agents':<10} {'N ops':>10} {'Time(s)':>10} {'Throughput':>15}")
    print("-" * 50)
    for label, metrics in scaling.items():
        print(f"{label:<10} {metrics['n_ops']:>10} {metrics['time_s']:>10.3f} "
              f"{metrics['throughput_ops_s']:>15.0f}")

    # 6. Ablation study
    print("\n--- Ablation Study (100K ops, 16 agents) ---")
    ablation = run_ablation(n_ops=100_000, n_agents=16)
    print(f"{'Configuration':<20} {'Entities':>10} {'Dupes':>8} {'Orphans':>8} {'Redirects':>10} {'Time(ms)':>10}")
    print("-" * 70)
    for name, metrics in ablation.items():
        print(f"{name:<20} {metrics['entities']:>10} {metrics['duplicates']:>8} "
              f"{metrics['orphans']:>8} {metrics['redirects']:>10} {metrics['time_ms']:>10.1f}")

    print("\n" + "=" * 100)
    print("Evaluation complete.")
    print("=" * 100)


if __name__ == "__main__":
    main()
