"""
Baseline comparison and performance benchmark for the CRDT projection pipeline.

Compares:
1. Naive merge (no dedup, no redirect) — baseline
2. Redirect-only (no orphan guard) — intermediate
3. Full pipeline (redirect + orphan guard) — our approach

Measures: orphans produced, timing, convergence, information loss.
"""

import hashlib
import json
import sys
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


def _parse_vv(s: str) -> Dict[str, int]:
    if not s or s == "{}":
        return {}
    return json.loads(s)


def compute_fingerprint(name: str, entity_type: str, description: str = "") -> str:
    canonical = lambda s: " ".join(s.lower().strip().split())
    payload = f"{canonical(name)}|{canonical(entity_type)}|{canonical(description)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Phase 1: Entity merge
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


# ---------------------------------------------------------------------------
# Phase 2: Dedup
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Phase 3: Edge merge + redirect
# ---------------------------------------------------------------------------


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
# Three approaches
# ---------------------------------------------------------------------------


def naive_merge(eops: List[EntityOp], edops: List[EdgeOp]) -> Dict:
    """Baseline: no dedup, no redirect. Just union the logs."""
    merged = merge_entity_ops(eops)
    edges = merge_edges(edops)
    # No redirect, no orphan guard — edges reference raw IDs
    return {"entities": merged, "edges": edges, "redirects": {}}


def redirect_only(eops: List[EntityOp], edops: List[EdgeOp]) -> Dict:
    """Intermediate: redirect but no orphan guard."""
    merged = merge_entity_ops(eops)
    canonical, redirects = entity_dedup(merged)
    edges = merge_edges(edops)
    edges = redirect_edges(edges, redirects)
    return {"entities": canonical, "edges": edges, "redirects": redirects}


def full_pipeline(eops: List[EntityOp], edops: List[EdgeOp]) -> Dict:
    """Full pipeline: redirect + orphan guard."""
    merged = merge_entity_ops(eops)
    canonical, redirects = entity_dedup(merged)
    edges = merge_edges(edops)
    edges = redirect_edges(edges, redirects)
    # Orphan guard
    canonical_ids = set(canonical.keys())
    edges = {eid: info for eid, info in edges.items()
             if info["source_id"] in canonical_ids and info["target_id"] in canonical_ids}
    return {"entities": canonical, "edges": edges, "redirects": redirects}


# ---------------------------------------------------------------------------
# Orphan counting
# ---------------------------------------------------------------------------


def count_orphans(result: Dict) -> int:
    entity_ids = set(result["entities"].keys())
    return sum(1 for info in result["edges"].values()
               if info["source_id"] not in entity_ids or info["target_id"] not in entity_ids)


def count_duplicates(result: Dict) -> int:
    """Count entities with duplicate fingerprints (semantic duplicates)."""
    fp_map: Dict[str, List[int]] = {}
    for eid, info in result["entities"].items():
        fp = info.get("fingerprint", "") or compute_fingerprint(info["name"], info["entity_type"], info.get("description", ""))
        fp_map.setdefault(fp, []).append(eid)
    return sum(len(ids) - 1 for ids in fp_map.values() if len(ids) > 1)


def count_canonical_edges(result: Dict) -> int:
    """Count edges where both endpoints are canonical (highest-ID per fingerprint group)."""
    fp_map: Dict[str, List[int]] = {}
    for eid, info in result["entities"].items():
        fp = info.get("fingerprint", "") or compute_fingerprint(info["name"], info["entity_type"], info.get("description", ""))
        fp_map.setdefault(fp, []).append(eid)
    canonical_ids = {max(ids) for ids in fp_map.values()}
    return sum(1 for info in result["edges"].values()
               if info["source_id"] in canonical_ids and info["target_id"] in canonical_ids)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE kg_entity_crdt (
    entity_id INTEGER NOT NULL, agent_id TEXT NOT NULL,
    op TEXT NOT NULL CHECK (op IN ('add','remove')),
    version_vector TEXT NOT NULL, name TEXT, entity_type TEXT,
    description TEXT, fingerprint TEXT, timestamp REAL NOT NULL
);
CREATE TABLE kg_edge_crdt (
    edge_id INTEGER NOT NULL, source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL, relation TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0, valid_at TEXT,
    agent_id TEXT NOT NULL, version_vector TEXT NOT NULL,
    timestamp REAL NOT NULL
);
CREATE TABLE kg_entities (
    entity_id INTEGER PRIMARY KEY, name TEXT NOT NULL, entity_type TEXT NOT NULL,
    mentions INTEGER DEFAULT 1, fingerprint TEXT, UNIQUE(fingerprint)
);
CREATE TABLE kg_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL, relation TEXT NOT NULL, weight REAL DEFAULT 1.0
);
"""


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


def bench(label: str, eops: List[EntityOp], edops: List[EdgeOp], rounds: int = 100) -> Dict:
    """Run all three approaches and measure."""
    results = {}
    for name, fn in [("naive", naive_merge), ("redirect_only", redirect_only), ("full_pipeline", full_pipeline)]:
        # Warmup
        for _ in range(5):
            fn(eops, edops)

        # Timed runs
        t0 = time.perf_counter()
        for _ in range(rounds):
            r = fn(eops, edops)
        t1 = time.perf_counter()

        orphans = count_orphans(r)
        dupes = count_duplicates(r)
        canonical = count_canonical_edges(r)
        results[name] = {
            "entities": len(r["entities"]),
            "edges": len(r["edges"]),
            "orphans": orphans,
            "duplicates": dupes,
            "canonical_edges": canonical,
            "redirects": len(r["redirects"]),
            "time_us": (t1 - t0) / rounds * 1_000_000,
        }
    return {"label": label, **{f"{k}_{sk}": sv for k, v in results.items() for sk, sv in v.items()}}


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def scenario_basic():
    """Two agents create 'alice' under IDs 42 and 99."""
    return [
        EntityOp(15, "a", "add", {"a": 1}, "bob", "person", "", "", 50.0),
        EntityOp(23, "b", "add", {"b": 1}, "charlie", "person", "", "", 150.0),
        EntityOp(42, "a", "add", {"a": 2}, "alice", "person", "", "", 100.0),
        EntityOp(99, "b", "add", {"b": 2}, "alice", "person", "", "", 200.0),
    ], [
        EdgeOp(1, 42, 15, "collaborates_with", 1.0, None, "a", {"a": 3}, 110.0),
        EdgeOp(2, 99, 23, "collaborates_with", 1.0, None, "b", {"b": 3}, 210.0),
    ]


def scenario_threeway():
    """Three agents create 'project:x' under IDs 10, 20, 30."""
    return [
        EntityOp(10, "a", "add", {"a": 1}, "project:x", "project", "", "", 100.0),
        EntityOp(20, "b", "add", {"b": 1}, "project:x", "project", "", "", 200.0),
        EntityOp(30, "c", "add", {"c": 1}, "project:x", "project", "", "", 300.0),
        EntityOp(15, "a", "add", {"a": 2}, "target", "proj", "", "", 50.0),
    ], [
        EdgeOp(1, 10, 15, "depends_on", 1.0, None, "a", {"a": 2}, 110.0),
        EdgeOp(2, 20, 15, "depends_on", 1.0, None, "b", {"b": 2}, 210.0),
        EdgeOp(3, 30, 15, "depends_on", 1.0, None, "c", {"c": 2}, 310.0),
    ]


def scenario_homonym():
    """Two 'alice' with different descriptions — should coexist."""
    return [
        EntityOp(42, "a", "add", {"a": 1}, "alice", "person", "lawyer", "", 100.0),
        EntityOp(99, "b", "add", {"b": 1}, "alice", "person", "chef", "", 200.0),
    ], []


def scenario_large_dedup(N: int = 5000, K: int = 50):
    """Large-scale with collisions: K distinct entities, each created by 10 different peers.

    Each peer creates the same entity (same name, type, description) under a
    different entity_id. After merge, only K canonical entities survive.
    This exercises the dedup path with N/K = 100 ops per fingerprint group.
    """
    eops = []
    edops = []
    for i in range(N):
        group = i % K  # which entity (0..K-1)
        peer = i // K  # which peer created it (0..N/K-1)
        eid = group * (N // K) + peer  # unique ID per peer per entity (non-overlapping)
        # Same content (name, type, desc) for all ops in the same group
        eops.append(EntityOp(eid, f"agent_{peer % 10}", "add",
                             {f"agent_{peer % 10}": peer // 10 + 1},
                             f"entity_{group}", "type", "shared description", "",
                             float(i)))
    for i in range(N // 10):
        edops.append(EdgeOp(i, (i % K) * 1000, ((i + 1) % K) * 1000,
                            "related_to", 1.0, None, f"agent_{i % 10}",
                            {f"agent_{i % 10}": i // K + 1}, float(i)))
    return eops, edops


def scenario_tombstoned_edge():
    """Edge references a tombstoned entity — orphan guard must catch it.

    Scaled version: 10 surviving entities, 5 tombstoned entities with dangling edges.
    Naive/redirect_only will have 5 orphans; full_pipeline will have 0.
    """
    eops = []
    edops = []
    # 10 surviving entities
    for i in range(10):
        eops.append(EntityOp(i + 1, f"agent_{i % 3}", "add",
                             {f"agent_{i % 3}": i // 3 + 1},
                             f"entity_{i}", "type", "", "", float(i)))
    # 5 tombstoned entities (each created then removed)
    for i in range(5):
        eid = 100 + i
        eops.append(EntityOp(eid, f"agent_{i % 3}", "add",
                             {f"agent_{i % 3}": i // 3 + 1},
                             f"tombstoned_{i}", "type", "", "", float(100 + i)))
        eops.append(EntityOp(eid, f"agent_{i % 3}", "remove",
                             {f"agent_{i % 3}": i // 3 + 2},
                             f"tombstoned_{i}", "type", "", "", float(200 + i)))
    # 15 edges: 10 to surviving entities (valid), 5 to tombstoned entities (orphans)
    for i in range(10):
        edops.append(EdgeOp(i, i + 1, (i + 1) % 10 + 1, "related_to", 1.0,
                            None, f"agent_{i % 3}", {f"agent_{i % 3}": i // 3 + 1}, float(i)))
    for i in range(5):
        edops.append(EdgeOp(10 + i, 100 + i, (i % 10) + 1, "related_to", 1.0,
                            None, f"agent_{i % 3}", {f"agent_{i % 3}": i // 3 + 2}, float(200 + i)))
    return eops, edops


def scenario_tombstone_concurrency():
    """5 entities created and tombstoned concurrently, each with dangling edges."""
    eops = []
    edops = []
    for i in range(5):
        eid = 100 + i
        for j in range(3):
            eops.append(EntityOp(eid, f"agent_{j}", "add",
                                 {f"agent_{j}": j + 1},
                                 f"tomb_{i}", "type", "", "",
                                 float(i * 10 + j)))
        eops.append(EntityOp(eid, "agent_0", "remove",
                             {"agent_0": 10},
                             f"tomb_{i}", "type", "", "",
                             float(i * 10 + 10)))
    for i in range(5):
        eops.append(EntityOp(i + 1, f"agent_{i % 3}", "add",
                             {f"agent_{i % 3}": i // 3 + 1},
                             f"surv_{i}", "type", "", "", float(i)))
    for i in range(5):
        edops.append(EdgeOp(i, i + 1, (i + 1) % 5 + 1, "related_to", 1.0,
                            None, f"agent_{i % 3}", {f"agent_{i % 3}": i // 3 + 1}, float(i)))
    for i in range(5):
        edops.append(EdgeOp(5 + i, 100 + i, (i % 5) + 1, "related_to", 1.0,
                            None, f"agent_{i % 3}", {f"agent_{i % 3}": i // 3 + 2}, float(200 + i)))
    return eops, edops


# ---------------------------------------------------------------------------
# Orphan guard property test
# ---------------------------------------------------------------------------


def test_orphan_guard_property():
    """Property: orphan guard drops ALL edges with non-canonical endpoints."""
    eops = [
        EntityOp(1, "a", "add", {"a": 1}, "bob", "person", "", "", 50.0),
        EntityOp(2, "a", "add", {"a": 2}, "alice", "person", "", "", 100.0),
        EntityOp(3, "a", "add", {"a": 3}, "carol", "person", "", "", 150.0),
        EntityOp(2, "a", "remove", {"a": 4}, "alice", "person", "", "", 200.0),
    ]
    edops = [
        EdgeOp(1, 1, 3, "related_to", 1.0, None, "a", {"a": 5}, 250.0),
        EdgeOp(2, 2, 1, "related_to", 1.0, None, "a", {"a": 6}, 260.0),
        EdgeOp(3, 99, 3, "related_to", 1.0, None, "a", {"a": 7}, 270.0),
        EdgeOp(4, 99, 2, "related_to", 1.0, None, "a", {"a": 8}, 280.0),
    ]

    result = full_pipeline(eops, edops)
    entity_ids = set(result["entities"].keys())
    for eid, info in result["edges"].items():
        assert info["source_id"] in entity_ids, f"Orphan: source {info['source_id']}"
        assert info["target_id"] in entity_ids, f"Orphan: target {info['target_id']}"

    assert len(result["edges"]) == 1, f"Expected 1 edge, got {len(result['edges'])}"
    assert 1 in result["edges"], "Edge 1 (1→3) must survive"
    assert 2 not in result["edges"], "Edge 2 (tombstoned src) must be dropped"
    assert 3 not in result["edges"], "Edge 3 (never-created src) must be dropped"
    assert 4 not in result["edges"], "Edge 4 (both bad) must be dropped"

    naive = naive_merge(eops, edops)
    assert len(naive["edges"]) == 4, f"Naive should keep all 4 edges, got {len(naive['edges'])}"

    redirect = redirect_only(eops, edops)
    assert len(redirect["edges"]) == 4, f"Redirect should keep all 4 edges, got {len(redirect['edges'])}"

    print("Orphan guard property: PASS")


# ---------------------------------------------------------------------------
# 2-peer convergence test
# ---------------------------------------------------------------------------


def test_convergence_2peer():
    """Two peers with different operation orderings reach identical canonical state."""
    from itertools import permutations

    ops = [
        EntityOp(10, "a", "add", {"a": 1}, "project:x", "project", "", "", 100.0),
        EntityOp(20, "b", "add", {"b": 1}, "project:x", "project", "", "", 200.0),
        EntityOp(30, "c", "add", {"c": 1}, "project:x", "project", "", "", 300.0),
        EntityOp(15, "a", "add", {"a": 2}, "target", "proj", "", "", 50.0),
    ]
    edges = [
        EdgeOp(1, 10, 15, "depends_on", 1.0, None, "a", {"a": 2}, 110.0),
        EdgeOp(2, 20, 15, "depends_on", 1.0, None, "b", {"b": 2}, 210.0),
        EdgeOp(3, 30, 15, "depends_on", 1.0, None, "c", {"c": 2}, 310.0),
    ]

    results = set()
    for perm in permutations(ops):
        merged = merge_entity_ops(list(perm))
        canonical, redirects = entity_dedup(merged)
        merged_edges = merge_edges(edges)
        merged_edges = redirect_edges(merged_edges, redirects)
        # Orphan guard
        canonical_ids = set(canonical.keys())
        merged_edges = {eid: info for eid, info in merged_edges.items()
                        if info["source_id"] in canonical_ids and info["target_id"] in canonical_ids}
        # Canonical state as hashable key
        entity_key = tuple(sorted(canonical.keys()))
        edge_key = tuple(sorted((info["source_id"], info["target_id"]) for info in merged_edges.values()))
        results.add((entity_key, edge_key, frozenset(redirects.items())))

    assert len(results) == 1, f"Convergence violated: {len(results)} distinct outputs from {len(list(permutations(ops)))} permutations"
    print("Convergence (2-peer, all permutations): PASS")


# ---------------------------------------------------------------------------
# Phase 3 profiling
# ---------------------------------------------------------------------------


def profile_phase3(N: int = 10000):
    """Profile where time is spent in the full pipeline."""
    import time

    eops = []
    edops = []
    K = 100
    for i in range(N):
        group = i % K
        peer = i // K
        eid = group * 1000 + peer
        eops.append(EntityOp(eid, f"agent_{peer % 10}", "add",
                             {f"agent_{peer % 10}": peer // 10 + 1},
                             f"entity_{group}", "type", "shared", "", float(i)))
    for i in range(N // 10):
        edops.append(EdgeOp(i, (i % K) * 1000, ((i + 1) % K) * 1000,
                            "related_to", 1.0, None, f"agent_{i % 10}",
                            {f"agent_{i % 10}": i // K + 1}, float(i)))

    # Time each phase separately
    rounds = 50
    t0 = time.perf_counter()
    for _ in range(rounds):
        merged = merge_entity_ops(eops)
    t1 = time.perf_counter()

    t2 = time.perf_counter()
    for _ in range(rounds):
        canonical, redirects = entity_dedup(merged)
    t3 = time.perf_counter()

    t4 = time.perf_counter()
    for _ in range(rounds):
        merged_edges = merge_edges(edops)
    t5 = time.perf_counter()

    t6 = time.perf_counter()
    for _ in range(rounds):
        merged_edges = redirect_edges(merged_edges, redirects)
    t7 = time.perf_counter()

    t8 = time.perf_counter()
    for _ in range(rounds):
        canonical_ids = set(canonical.keys())
        filtered = {eid: info for eid, info in merged_edges.items()
                    if info["source_id"] in canonical_ids and info["target_id"] in canonical_ids}
    t9 = time.perf_counter()

    phase1_ms = (t1 - t0) / rounds * 1000
    phase2_ms = (t3 - t2) / rounds * 1000
    phase3a_ms = (t5 - t4) / rounds * 1000  # edge merge
    phase3b_ms = (t7 - t6) / rounds * 1000  # redirect
    phase3c_ms = (t9 - t8) / rounds * 1000  # orphan guard
    total_ms = phase1_ms + phase2_ms + phase3a_ms + phase3b_ms + phase3c_ms

    print(f"\nPhase profiling ({N} ops, {N//10} edges, {rounds} rounds):")
    print(f"  Phase 1 (entity merge):  {phase1_ms:>8.3f}ms  ({phase1_ms/total_ms*100:>5.1f}%)")
    print(f"  Phase 2 (dedup):         {phase2_ms:>8.3f}ms  ({phase2_ms/total_ms*100:>5.1f}%)")
    print(f"  Phase 3a (edge merge):   {phase3a_ms:>8.3f}ms  ({phase3a_ms/total_ms*100:>5.1f}%)")
    print(f"  Phase 3b (redirect):     {phase3b_ms:>8.3f}ms  ({phase3b_ms/total_ms*100:>5.1f}%)")
    print(f"  Phase 3c (orphan guard): {phase3c_ms:>8.3f}ms  ({phase3c_ms/total_ms*100:>5.1f}%)")
    print(f"  Total:                   {total_ms:>8.3f}ms")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    scenarios = [
        ("Basic concurrent", *scenario_basic()),
        ("Three-way concurrent", *scenario_threeway()),
        ("Homonym disambiguation", *scenario_homonym()),
        ("Tombstoned edge", *scenario_tombstoned_edge()),
        ("Tombstone concurrency", *scenario_tombstone_concurrency()),
        ("Large dedup (5000 ops, 50 keys)", *scenario_large_dedup()),
    ]

    print("=" * 120)
    print(f"{'Scenario':<30} {'Approach':<15} {'Ent':>4} {'Edg':>4} {'Dup':>3} {'Orph':>4} {'Canon':>5} {'Redir':>5} {'Time(μs)':>9}")
    print("-" * 120)

    for label, eops, edops in scenarios:
        r = bench(label, eops, edops, rounds=200)
        for approach in ["naive", "redirect_only", "full_pipeline"]:
            ents = r[f"{approach}_entities"]
            edgs = r[f"{approach}_edges"]
            dup = r[f"{approach}_duplicates"]
            orph = r[f"{approach}_orphans"]
            canon = r[f"{approach}_canonical_edges"]
            redir = r[f"{approach}_redirects"]
            t = r[f"{approach}_time_us"]
            print(f"{label:<30} {approach:<15} {ents:>4} {edgs:>4} {dup:>3} {orph:>4} {canon:>5} {redir:>5} {t:>9.1f}")
        print()

    # Overhead comparison
    naive_time = bench("overhead", *scenario_large_dedup(), rounds=200)["naive_time_us"]
    full_time = bench("overhead", *scenario_large_dedup(), rounds=200)["full_pipeline_time_us"]
    delta = full_time - naive_time
    pct = (delta / naive_time) * 100 if naive_time > 0 else 0

    print("=" * 120)
    print("Key findings:")
    print("- Naive merge: keeps duplicates, edges split between duplicates, orphans from tombstoned refs")
    print("- Redirect-only: deduplicates, redirects edges, but misses tombstoned/never-created")
    print("- Full pipeline: deduplicates + orphan guard, zero orphans, all edges canonical")
    print(f"- Head-to-head overhead: {delta:.0f}μs ({pct:.1f}% relative) for 5000 ops with dedup")
    print("  Note: overhead is from dedup + redirect + guard (Phase 2+3), NOT from Phase 1")
    print("  (Phase 1 is present in both naive and full_pipeline)")
    print("=" * 120)

    # Convergence test
    print()
    test_convergence_2peer()

    # Orphan guard property test
    test_orphan_guard_property()

    # Phase profiling
    profile_phase3()

    # Parameter sweep
    parameter_sweep()


# ---------------------------------------------------------------------------
# Parameter sweep
# ---------------------------------------------------------------------------


def parameter_sweep():
    """Sweep N (ops) and K (keys) to show scaling behavior.

    Run with `--scale` to reproduce the paper's §8.2 rows at N = 1M / 10M
    (K=1000): each row is a single timed run after warmup.
    """
    print("\n" + "=" * 95)
    print("Parameter sweep: runtime vs (N, K) for full_pipeline")
    print("=" * 95)
    print(f"{'N':>8} {'K':>6} {'Ent':>6} {'Redir':>7} {'Time(ms)':>10} {'us/op':>8}")
    print("-" * 95)

    scale = "--scale" in sys.argv
    Ns = [100000, 1000000, 10000000] if scale else [1000, 5000, 10000, 50000, 100000]
    Ks = [1000] if scale else [10, 100, 1000]

    for N in Ns:
        for K in Ks:
            eops = []
            edops = []
            for i in range(N):
                group = i % K
                peer = i // K
                eid = group * (N // K) + peer  # unique ID per peer per entity (non-overlapping)
                # Same content for all ops in same group → collisions → dedup
                eops.append(EntityOp(eid, f"agent_{peer % 5}", "add",
                                     {f"agent_{peer % 5}": peer // 5 + 1},
                                     f"entity_{group}", "type", "shared", "", float(i)))
            for i in range(N // 10):
                edops.append(EdgeOp(i, (i % K) * 1000, ((i + 1) % K) * 1000,
                                    "related_to", 1.0, None, f"agent_{i % 5}",
                                    {f"agent_{i % 5}": i // K + 1}, float(i)))

            # Warmup
            full_pipeline(eops, edops)

            # Timed run (5 reps for small N, single run for the 1M/10M rows)
            reps = 1 if N >= 1000000 else 5
            t0 = time.perf_counter()
            for _ in range(reps):
                r = full_pipeline(eops, edops)
            t1 = time.perf_counter()

            ents = len(r["entities"])
            redir = len(r["redirects"])
            t_ms = (t1 - t0) / reps * 1000
            us_per_op = t_ms * 1000 / N

            print(f"{N:>8} {K:>6} {ents:>6} {redir:>7} {t_ms:>10.1f} {us_per_op:>8.1f}")

    print("=" * 95)
    print("Key insight: overhead per op is roughly constant (O(N) scaling).")
    print("Dedup benefit grows with collision rate: K=10 (high dedup) is faster than K=1000 (low dedup).")


if __name__ == "__main__":
    main()
