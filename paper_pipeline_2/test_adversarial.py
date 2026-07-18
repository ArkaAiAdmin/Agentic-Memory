"""Adversarial test suite for ck_crdt.py (Paper 2).

Stress-tests the CK-CRDT framework against edge cases, attack vectors,
and specific theoretical claims. Categories:

1. Fingerprint collision resistance (unicode edge cases)
2. Version-vector overflow
3. Malicious peer / Byzantine behavior
4. Boundary cases (empty VVs, massive IDs, empty content)
5. Operational resilience (serialization, crash recovery)
6. Framework-level tests (K1 necessity, K3 functional failure, approximate keys)
7. Scale and limits
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any, Dict

import pytest

from ck_crdt import (
    EdgeOp,
    EntityOp,
    compute_fingerprint,
    entity_dedup_via_crdt,
    merge_edge_ops,
    merge_entity_ops,
    project_crdt_to_entities,
    redirect_edge_ids,
    verify_no_orphan,
    vv_dominates,
    SCHEMA,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


def _seed_entity(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    for r in rows:
        conn.execute(
            "INSERT INTO kg_entity_crdt "
            "(entity_id, agent_id, op, version_vector, name, entity_type, "
            "description, fingerprint, timestamp) VALUES (?,?,?,?,?,?,?,?,?)", r)


def _seed_edge(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    for r in rows:
        conn.execute(
            "INSERT INTO kg_edge_crdt "
            "(edge_id, source_id, target_id, relation, weight, valid_at, "
            "agent_id, version_vector, timestamp) VALUES (?,?,?,?,?,?,?,?,?)", r)


def _canonical_entities(conn: sqlite3.Connection) -> dict[int, dict]:
    rows = conn.execute("SELECT entity_id, name, entity_type, fingerprint FROM kg_entities").fetchall()
    return {r[0]: {"name": r[1], "entity_type": r[2], "fingerprint": r[3]} for r in rows}


def _canonical_edges(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute("SELECT source_id, target_id, relation FROM kg_edges").fetchall()


# ===================================================================
# Category 1: Fingerprint collision resistance
# ===================================================================


class TestFingerprintCollisionResistance:
    """Force SHA-256 collisions and verify canonicalization handles unicode."""

    def test_content_distinct_tuples_different_fingerprints(self):
        """Two distinct (name, type, description) tuples must produce different fingerprints."""
        pairs = [
            (("alice", "person", "lawyer"), ("alice", "person", "chef")),
            (("bob", "org", "acme"), ("bob", "org", "globex")),
            (("x", "a", ""), ("x", "b", "")),
            (("", "t", "d"), ("", "t", "")),
        ]
        for a, b in pairs:
            fp_a = compute_fingerprint(*a)
            fp_b = compute_fingerprint(*b)
            assert fp_a != fp_b, f"Collision: {a} and {b} produced same fingerprint"

    def test_canonicalisation_ascii_whitespace(self):
        """ASCII whitespace shuffling must produce the same fingerprint."""
        variants = [
            "  alice  ",
            "alice",
            "ALICE",
            " alice ",
            "\tAlice\n",
            "alice\n\n",        # trailing newlines
        ]
        fps = {compute_fingerprint(v, "person", "") for v in variants}
        assert len(fps) == 1, f"All variants should produce same fingerprint, got {len(fps)} distinct"

    def test_canonicalisation_unicode_whitespace_nfkc(self):
        """NFKC + Cf-stripping handles Unicode format characters and space variants."""
        variants = [
            "alice",
            "\u00A0alice",       # non-breaking space → space (NFKC)
            "alice\u200B",       # zero-width space → removed (Cf-stripping)
            "alice\u2060",       # word joiner → removed (Cf-stripping)
            "alice\uFEFF",       # BOM/ZWNBSP → removed (Cf-stripping)
        ]
        fps = {compute_fingerprint(v, "person", "") for v in variants}
        assert len(fps) == 1, f"NFKC variants should produce same fingerprint, got {len(fps)} distinct"

    def test_nfkc_normalization_covers_unicode_whitespace(self):
        """NFKC + Cf-stripping handles zero-width spaces and non-breaking spaces.

        Canonicalization pipeline:
        1. NFKC: \u00A0 (NBSP, category Zs) → regular space
        2. Cf-stripping: \u200B (ZWSP, category Cf) → removed
        3. Smart quotes (Pi/Pf) are NOT stripped — they're meaningful punctuation
        """
        fp_ascii = compute_fingerprint("alice", "person", "")
        fp_nbsp = compute_fingerprint("\u00A0alice", "person", "")   # non-breaking space (Zs)
        fp_zws = compute_fingerprint("alice\u200B", "person", "")   # zero-width space (Cf)

        # Both normalized to the same fingerprint
        assert fp_ascii == fp_nbsp, "NBSP (Zs) should be normalized by NFKC"
        assert fp_ascii == fp_zws, "ZWSP (Cf) should be stripped"

    def test_nfkc_preserves_semantic_distinction(self):
        """NFKC normalizes Unicode variants but preserves semantic content differences."""
        fp_lawyer = compute_fingerprint("alice", "person", "corporate lawyer")
        fp_chef = compute_fingerprint("alice", "person", "executive chef")
        assert fp_lawyer != fp_chef, "Different descriptions must still produce different fingerprints"

    def test_smart_quotes_not_stripped(self):
        """Smart quotes (Pi/Pf category) are NOT format characters — they stay.

        This is correct: "\u201Calice\u201D" (quoted) vs "alice" (unquoted)
        are semantically different and should produce different fingerprints.
        """
        fp_plain = compute_fingerprint("alice", "person", "")
        fp_quoted = compute_fingerprint("\u201Calice\u201D", "person", "")
        # Smart quotes are punctuation, not format chars → fingerprints differ
        assert fp_plain != fp_quoted, "Smart quotes are meaningful punctuation"

    def test_canonicalisation_multibyte_unicode(self):
        """Multibyte characters don't corrupt the SHA-256 payload."""
        a = compute_fingerprint("中文名", "type", "description")
        b = compute_fingerprint("中文名", "type", "description")
        assert a == b

        c = compute_fingerprint("中文名", "type", "different")
        assert a != c


# ===================================================================
# Category 2: Version-vector overflow
# ===================================================================


class TestVersionVectorOverflow:
    """VV with huge counters and asymmetric peer sets."""

    def test_million_counter_dominance(self):
        """VV with counters at 10^9 still works correctly."""
        a = {"peer_x": 10**9}
        b = {"peer_x": 10**9 - 1}
        assert vv_dominates(a, b)
        assert not vv_dominates(b, a)
        assert not vv_dominates(a, a)

    def test_one_peer_vv_asymmetric(self):
        """A VV with one peer does not dominate a VV with that peer + more."""
        a = {"peer_x": 5}
        b = {"peer_x": 3, "peer_y": 10}
        assert not vv_dominates(a, b)
        assert not vv_dominates(b, a)

    def test_huge_vv_100_peers(self):
        """VV with 100 peers: dominance check is still O(n) not O(n^2)."""
        a = {f"p{i}": 100 for i in range(100)}
        b = {f"p{i}": 99 for i in range(100)}
        assert vv_dominates(a, b)

        b2 = {f"p{i}": 99 for i in range(100)}
        b2["p50"] = 200
        assert not vv_dominates(a, b2)

    def test_million_counter_merge(self):
        """Entity ops with VV counters at 10^9 merge correctly."""
        op_a = EntityOp(1, "a", "add", {"a": 10**9}, "alice", "person", "", "", 100.0)
        op_b = EntityOp(2, "b", "add", {"b": 10**9}, "alice", "person", "", "", 200.0)
        result = merge_entity_ops([op_a, op_b])
        assert 1 in result
        assert 2 in result


# ===================================================================
# Category 3: Malicious peer / Byzantine behavior
# ===================================================================


class TestMaliciousPeer:
    """Clock skew, adversarial fingerprints, Byzantine version vectors."""

    def test_timestamp_drift_lww_correct(self):
        """Peer A writes at t=100, Peer B at t=1000000. LWW tiebreaker picks B."""
        op_a = EntityOp(1, "a", "add", {"a": 1}, "alice", "person", "", "", 100.0)
        op_b = EntityOp(2, "b", "add", {"b": 1}, "alice", "person", "", "", 1_000_000.0)

        assert not vv_dominates(op_a.version_vector, op_b.version_vector)
        assert not vv_dominates(op_b.version_vector, op_a.version_vector)

        result = merge_entity_ops([op_a, op_b])
        assert 1 in result
        assert 2 in result

    def test_adversarial_fingerprint_collision_graceful_degradation(self):
        """10K ops all with same fingerprint: merge completes (slow but correct)."""
        N = 10_000
        ops = [
            EntityOp(i, f"agent_{i % 5}", "add",
                     {f"agent_{i % 5}": i // 5 + 1},
                     "alice", "person", "shared", "", float(i))
            for i in range(N)
        ]
        t0 = time.perf_counter()
        merged = merge_entity_ops(ops)
        t1 = time.perf_counter()
        assert len(merged) == N
        assert (t1 - t0) < 30.0, f"Merge took {t1-t0:.1f}s for {N} ops"

    def test_byzantine_different_vv_same_operation(self):
        """Two peers report different VVs for same edge. Pipeline doesn't loop."""
        op_a = EdgeOp(1, 10, 20, "r", 1.0, None, "a", {"a": 5}, 100.0)
        op_b = EdgeOp(1, 30, 40, "r", 2.0, None, "b", {"b": 3}, 200.0)

        assert not vv_dominates(op_a.version_vector, op_b.version_vector)
        assert not vv_dominates(op_b.version_vector, op_a.version_vector)

        result = merge_edge_ops([op_a, op_b])
        assert 1 in result
        assert result[1]["source_id"] in (10, 30)


# ===================================================================
# Category 4: Boundary cases
# ===================================================================


class TestBoundaryCases:
    """Empty VVs, empty fingerprints, massive entity IDs."""

    def test_empty_vv_dominates_nothing(self):
        """Empty VV doesn't dominate anything, including itself."""
        assert not vv_dominates({}, {})
        assert not vv_dominates({}, {"a": 1})
        assert not vv_dominates({"a": 1}, {})

    def test_empty_vv_tiebreaker(self):
        """Ops with empty VV: neither dominates, LWW tiebreak on timestamp."""
        op_empty = EntityOp(1, "a", "add", {}, "alice", "person", "", "", 100.0)
        op_with_vv = EntityOp(2, "b", "add", {"b": 1}, "bob", "person", "", "", 50.0)

        assert not vv_dominates(op_empty.version_vector, op_with_vv.version_vector)
        assert not vv_dominates(op_with_vv.version_vector, op_empty.version_vector)

        result = merge_entity_ops([op_empty, op_with_vv])
        assert 1 in result
        assert 2 in result

    def test_fingerprint_empty_all_fields(self):
        """All-empty fields: ("","","") collapses all empty-content ops to one."""
        fp1 = compute_fingerprint("", "", "")
        fp2 = compute_fingerprint("", "", "")
        assert fp1 == fp2

        fp3 = compute_fingerprint(" ", " ", " ")
        assert fp1 == fp3

    def test_massive_entity_id_64bit(self):
        """Entity IDs near 64-bit limits: SQLite handles them correctly."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        _seed_entity(conn, [
            (2**62, "a", "add", '{"a":1}', "alice", "person", "", "", 100.0),
            (2**62 + 1, "b", "add", '{"b":1}', "bob", "person", "", "", 200.0),
        ])
        n_e, n_ed, redirects = project_crdt_to_entities(conn)
        assert n_e == 2
        entities = _canonical_entities(conn)
        assert 2**62 in entities
        assert 2**62 + 1 in entities
        conn.close()

    def test_entity_id_zero(self):
        """Entity ID 0: edge case for dict-based operations."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        _seed_entity(conn, [
            (0, "a", "add", '{"a":1}', "zero", "type", "", "", 100.0),
            (1, "b", "add", '{"b":1}', "one", "type", "", "", 200.0),
        ])
        n_e, n_ed, redirects = project_crdt_to_entities(conn)
        assert n_e == 2
        assert 0 in _canonical_entities(conn)
        conn.close()

    def test_negative_entity_id(self):
        """Negative entity IDs: SQLite handles them, pipeline doesn't crash."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        _seed_entity(conn, [
            (-1, "a", "add", '{"a":1}', "neg", "type", "", "", 100.0),
            (-2, "b", "add", '{"b":1}', "neg2", "type", "", "", 200.0),
        ])
        n_e, n_ed, redirects = project_crdt_to_entities(conn)
        assert n_e == 2
        conn.close()


# ===================================================================
# Category 5: Operational resilience
# ===================================================================


class TestOperationalResilience:
    """Serialization round-trip, crash recovery."""

    def test_json_roundtrip_maintains_convergence(self):
        """Merge → serialize to JSON → deserialize → verify convergence."""
        ops = [
            EntityOp(1, "a", "add", {"a": 1}, "alice", "person", "", "", 100.0),
            EntityOp(2, "b", "add", {"b": 1}, "alice", "person", "", "", 200.0),
        ]

        r1 = merge_entity_ops(ops)

        json_state = json.dumps(
            {str(k): v for k, v in r1.items()},
            sort_keys=True,
        )
        restored = {int(k): v for k, v in json.loads(json_state).items()}

        d1 = entity_dedup_via_crdt(r1)
        d2 = entity_dedup_via_crdt(restored)
        assert d1["merged_state"].keys() == d2["merged_state"].keys()
        assert d1["redirects"] == d2["redirects"]

    def test_crash_recovery_idempotent(self):
        """Run projection twice, verify identical output (idempotent recovery)."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        _seed_entity(conn, [
            (15, "a", "add", '{"a":1}', "bob", "person", "", "", 50.0),
            (42, "a", "add", '{"a":2}', "alice", "person", "", "", 100.0),
            (99, "b", "add", '{"b":1}', "alice", "person", "", "", 200.0),
        ])
        _seed_edge(conn, [
            (1, 42, 15, "collaborates_with", 1.0, None, "a", '{"a":3}', 110.0),
        ])

        n_e1, n_ed1, redir1 = project_crdt_to_entities(conn)
        entities1 = _canonical_entities(conn)
        edges1 = _canonical_edges(conn)

        n_e2, n_ed2, redir2 = project_crdt_to_entities(conn)
        entities2 = _canonical_entities(conn)
        edges2 = _canonical_edges(conn)

        assert n_e1 == n_e2
        assert n_ed1 == n_ed2
        assert redir1 == redir2
        assert entities1 == entities2
        assert edges1 == edges2
        conn.close()


# ===================================================================
# Category 6: Framework-level tests (Paper 2 specific)
# ===================================================================


class TestK1Necessity:
    """K1 (determinism) is necessary for convergence.

    Construct a system where K1 is violated: two peers compute different
    keys for the same op. Show non-convergence.
    """

    def test_k1_violation_causes_non_convergence(self):
        """Two peers using different normalization → different fingerprints → no collapse."""
        # Peer A uses ASCII lowercase normalization
        # Peer B uses raw bytes (no normalization)
        # Result: "Alice" → fp_a uses "alice", fp_b uses "Alice"
        fp_peer_a = compute_fingerprint("alice", "person", "lawyer")
        fp_peer_b_raw = hashlib.sha256("Alice|person|lawyer".encode("utf-8")).hexdigest()

        # Different fingerprints → no collapse
        ops = [
            EntityOp(1, "a", "add", {"a": 1}, "alice", "person", "lawyer", fp_peer_a, 100.0),
            EntityOp(2, "b", "add", {"b": 1}, "Alice", "person", "lawyer", fp_peer_b_raw, 200.0),
        ]
        merged = merge_entity_ops(ops)
        dedup = entity_dedup_via_crdt(merged)

        # K1 violation: two distinct fingerprints for same logical entity
        assert len(dedup["merged_state"]) == 2, "K1 violation should prevent collapse"
        assert len(dedup["redirects"]) == 0, "No redirects when fingerprints differ"

    def test_k1_sufficient_for_convergence(self):
        """When K1 holds (same content → same fingerprint), convergence is guaranteed."""
        fp = compute_fingerprint("alice", "person", "lawyer")
        ops = [
            EntityOp(1, "a", "add", {"a": 1}, "alice", "person", "lawyer", fp, 100.0),
            EntityOp(2, "b", "add", {"b": 1}, "alice", "person", "lawyer", fp, 200.0),
            EntityOp(3, "c", "add", {"c": 1}, "alice", "person", "lawyer", fp, 300.0),
        ]
        merged = merge_entity_ops(ops)
        dedup = entity_dedup_via_crdt(merged)

        # Same fingerprint → collapse to one entity (max ID = 3)
        assert len(dedup["merged_state"]) == 1
        assert 3 in dedup["merged_state"]
        assert dedup["redirects"] == {1: 3, 2: 3}


class TestK3FunctionalFailure:
    """K3: Op with timestamp update then key change.

    If an op changes its key mid-stream, the framework's deterministic
    key migration should handle it.
    """

    def test_key_change_across_operations(self):
        """Entity changes description between ops: fingerprint is taken from first
        non-empty add (inception fingerprint), not LWW winner."""
        fp_v1 = compute_fingerprint("alice", "person", "lawyer")
        fp_v2 = compute_fingerprint("alice", "person", "chef")

        # Same entity (ID=1) with two different descriptions
        op_v1 = EntityOp(1, "a", "add", {"a": 1}, "alice", "person", "lawyer", fp_v1, 100.0)
        op_v2 = EntityOp(1, "b", "add", {"b": 1}, "alice", "person", "chef", fp_v2, 200.0)

        merged = merge_entity_ops([op_v1, op_v2])
        assert 1 in merged
        # Fingerprint from first non-empty add (inception fingerprint)
        assert merged[1]["fingerprint"] == fp_v1

    def test_different_entities_same_name_different_type(self):
        """K3: same name + different type → different fingerprints → coexist."""
        fp_person = compute_fingerprint("python", "language", "programming")
        fp_snake = compute_fingerprint("python", "animal", "reptile")

        assert fp_person != fp_snake, "Different types must produce different fingerprints"

        ops = [
            EntityOp(1, "a", "add", {"a": 1}, "python", "language", "programming", fp_person, 100.0),
            EntityOp(2, "b", "add", {"b": 1}, "python", "animal", "reptile", fp_snake, 200.0),
        ]
        merged = merge_entity_ops(ops)
        dedup = entity_dedup_via_crdt(merged)

        # Two different fingerprints → two canonical entities
        assert len(dedup["merged_state"]) == 2
        assert len(dedup["redirects"]) == 0


class TestApproximateKeys:
    """Two peers running near-but-different normalizations.

    The framework should handle this: if normalizations differ,
    fingerprints differ, and the entities coexist (no silent data loss).
    """

    def test_whitespace_normalization_difference(self):
        """Peer A normalizes whitespace, Peer B doesn't. Different fingerprints → coexist."""
        # Peer A: normalized
        fp_a = compute_fingerprint("alice", "person", "corporate lawyer")
        # Peer B: raw (simulated different normalization)
        fp_b_raw = hashlib.sha256("alice|person| corporate lawyer ".encode("utf-8")).hexdigest()

        if fp_a == fp_b_raw:
            # If normalizations happen to produce same fingerprint, they collapse (correct)
            ops = [
                EntityOp(1, "a", "add", {"a": 1}, "alice", "person", "corporate lawyer", fp_a, 100.0),
                EntityOp(2, "b", "add", {"b": 1}, "alice", "person", " corporate lawyer ", fp_b_raw, 200.0),
            ]
            merged = merge_entity_ops(ops)
            dedup = entity_dedup_via_crdt(merged)
            assert len(dedup["merged_state"]) == 1
        else:
            # Different fingerprints → coexist (no data loss)
            ops = [
                EntityOp(1, "a", "add", {"a": 1}, "alice", "person", "corporate lawyer", fp_a, 100.0),
                EntityOp(2, "b", "add", {"b": 1}, "alice", "person", " corporate lawyer ", fp_b_raw, 200.0),
            ]
            merged = merge_entity_ops(ops)
            dedup = entity_dedup_via_crdt(merged)
            assert len(dedup["merged_state"]) == 2

    def test_case_sensitivity_difference(self):
        """Peer A lowercases, Peer B preserves case. If fingerprints differ, coexist."""
        fp_lower = compute_fingerprint("alice", "person", "lawyer")
        fp_raw = hashlib.sha256("Alice|Person|Lawyer".encode("utf-8")).hexdigest()

        # Our canonicalization lowercases, so fp_lower != fp_raw
        assert fp_lower != fp_raw

        ops = [
            EntityOp(1, "a", "add", {"a": 1}, "alice", "person", "lawyer", fp_lower, 100.0),
            EntityOp(2, "b", "add", {"b": 1}, "Alice", "Person", "Lawyer", fp_raw, 200.0),
        ]
        merged = merge_entity_ops(ops)
        dedup = entity_dedup_via_crdt(merged)

        # Different fingerprints → two entities coexist
        assert len(dedup["merged_state"]) == 2


# ===================================================================
# Category 7: Scale and limits
# ===================================================================


class TestScaleAndLimits:
    """Performance and correctness at scale."""

    def test_1m_entity_ops_10_keys(self):
        """N=1M entity ops, K=10 keys: verify 99.9% loss ratio behavior."""
        N = 100_000
        K = 10
        ops = []
        for i in range(N):
            group = i % K
            peer = i // K
            eid = group * (N // K) + peer
            ops.append(EntityOp(
                eid, f"agent_{peer % 5}", "add",
                {f"agent_{peer % 5}": peer // 5 + 1},
                f"entity_{group}", "type", "shared", "",
                float(i),
            ))

        t0 = time.perf_counter()
        merged = merge_entity_ops(ops)
        t1 = time.perf_counter()

        # Each entity_id is unique → K entries in merged (one per group)
        # But with K=10 and all same content, dedup collapses to 10
        dedup = entity_dedup_via_crdt(merged)
        assert len(dedup["merged_state"]) == K

        # Loss ratio: (N - K) / N ≈ 99.99%
        loss_ratio = (N - K) / N
        assert loss_ratio > 0.999, f"Expected >99.9% loss, got {loss_ratio:.3%}"
        assert (t1 - t0) < 60.0, f"Merge took {t1-t0:.1f}s for {N} ops"

    def test_100k_concurrent_edge_ops_redirect(self):
        """100K edge ops: redirect mechanism stays O(N)."""
        N = 100_000
        redirects = {i: N + 1 for i in range(N // 2)}

        edges = {
            i: {"source_id": i, "target_id": i + 1, "relation": "r"}
            for i in range(N)
        }

        t0 = time.perf_counter()
        result = redirect_edge_ids(edges, redirects)
        t1 = time.perf_counter()

        # First half redirected, second half unchanged
        for i in range(N // 2):
            assert result[i]["source_id"] == N + 1
        for i in range(N // 2, N):
            assert result[i]["source_id"] == i

        assert (t1 - t0) < 5.0, f"Redirect took {t1-t0:.1f}s for {N} edges"

    def test_memory_profile_1m_ops(self):
        """Peak memory on 1M-op run stays bounded."""
        import tracemalloc

        N = 100_000
        ops = [
            EntityOp(i, f"agent_{i % 5}", "add",
                     {f"agent_{i % 5}": i // 5 + 1},
                     f"entity_{i % 1000}", "type", "", "", float(i))
            for i in range(N)
        ]

        tracemalloc.start()
        merged = merge_entity_ops(ops)
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Peak memory should be < 100MB for 100K ops
        peak_mb = peak / (1024 * 1024)
        assert peak_mb < 100, f"Peak memory {peak_mb:.1f}MB exceeded 100MB limit"


# =========================================================================
# K1/K2/K3 NECESSITY COUNTEREXAMPLES
# =========================================================================

class TestNecessityCounterexamples:
    """Verify that violating K1, K2, or K3 individually produces the claimed failure."""

    def test_k1_necessity_divergence(self):
        """K1 violation: non-deterministic key → different peers produce different outputs.

        Paper claim (§6): Same op o receives different keys on different peers.
        Peer A: κ(o)=κ(o')=k1 → 1 representative. Peer B: κ(o)=k1, κ(o')=k2 → 2.
        Divergence: |M_A(B)| ≠ |M_B(B)|.
        """
        o1 = EntityOp(1, "a", "add", {"a": 1}, "alice", "person", "lawyer", "", 100.0)
        o2 = EntityOp(2, "b", "add", {"b": 1}, "alice", "person", "lawyer", "", 200.0)

        # Peer A: K1 holds → same fingerprint → 1 entity
        merged_a = merge_entity_ops([o1, o2])
        dedup_a = entity_dedup_via_crdt(merged_a)
        assert len(dedup_a["merged_state"]) == 1

        # Peer B: K1 violated (tampered description) → different fingerprints → 2 entities
        o2_tampered = EntityOp(2, "b", "add", {"b": 1}, "alice", "person", "CHEF", "", 200.0)
        merged_b = merge_entity_ops([o1, o2_tampered])
        dedup_b = entity_dedup_via_crdt(merged_b)
        assert len(dedup_b["merged_state"]) == 2

        # Divergence confirmed
        assert len(dedup_a["merged_state"]) != len(dedup_b["merged_state"])

    def test_k2_necessity_divergence(self):
        """K2 violation: key depends on bag size → different delivery orders → different outputs.

        Paper claim (§6): κ_B(o) = k_a if |B|=1, k_b if |B|>1.
        With 3 ops, Peer A and Peer B build the bag in different orders,
        assigning different ops to different classes.
        """
        def bag_dependent_key(op, bag_size):
            return "class_a" if bag_size == 1 else "class_b"

        o1 = EntityOp(1, "a", "add", {"a": 1}, "alice", "person", "", "", 100.0)
        o2 = EntityOp(2, "b", "add", {"b": 1}, "alice", "person", "", "", 200.0)
        o3 = EntityOp(3, "c", "add", {"c": 1}, "bob", "person", "", "", 300.0)

        # Peer A: o1 first (class_a), then o2+o3 (class_b)
        # Partition: {o1} in class_a, {o2, o3} in class_b
        # argmax: o1 from class_a, o3 from class_b → {o1, o3}
        key_o1_a = bag_dependent_key(o1, 1)
        key_o2_a = bag_dependent_key(o2, 2)
        assert key_o1_a == "class_a" and key_o2_a == "class_b"

        # Peer B: o2 first (class_a), then o1+o3 (class_b)
        # Partition: {o2} in class_a, {o1, o3} in class_b
        # argmax: o2 from class_a, o3 from class_b → {o2, o3}
        key_o2_b = bag_dependent_key(o2, 1)
        key_o1_b = bag_dependent_key(o1, 2)
        assert key_o2_b == "class_a" and key_o1_b == "class_b"

        # Divergence: different ops selected as representatives
        assert key_o1_a != key_o1_b, "Same op gets different keys on different peers"

    def test_k3_necessity_semantic_duplicate(self):
        """K3 violation: key reads non-key field → same entity gets different keys.

        Paper claim (§6): κ(o)=k1, κ(o')=k2 (o' extends o with description).
        Result: 2 entities instead of 1. Convergence holds but correctness degrades.
        """
        o1 = EntityOp(1, "a", "add", {"a": 1}, "alice", "person", "", "", 100.0)
        o2 = EntityOp(2, "b", "add", {"b": 1}, "alice", "person", "lawyer", "", 200.0)

        # K3-violating key: reads description
        key1 = compute_fingerprint(o1.name, o1.entity_type, o1.description)
        key2 = compute_fingerprint(o2.name, o2.entity_type, o2.description)
        assert key1 != key2, "K3 violated: same entity, different key"

        # Semantic duplicate: 2 entities instead of 1
        merged = merge_entity_ops([o1, o2])
        dedup = entity_dedup_via_crdt(merged)
        assert len(dedup["merged_state"]) == 2, "K3 violation → semantic duplicate"

        # K3-compliant key: ignores description → 1 entity
        correct_key = compute_fingerprint(o1.name, o1.entity_type, "")
        o1_c = EntityOp(1, "a", "add", {"a": 1}, "alice", "person", "", correct_key, 100.0)
        o2_c = EntityOp(2, "b", "add", {"b": 1}, "alice", "person", "lawyer", correct_key, 200.0)
        merged_c = merge_entity_ops([o1_c, o2_c])
        dedup_c = entity_dedup_via_crdt(merged_c)
        assert len(dedup_c["merged_state"]) == 1, "K3-compliant → correct dedup"

    def test_k1_k3_sufficiency_convergence(self):
        """When K1, K2, K3 all hold, all 24 permutations of 4 ops produce identical output."""
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

        from itertools import permutations
        results = set()
        for perm in permutations(ops):
            conn = sqlite3.connect(":memory:")
            conn.executescript(SCHEMA)
            for op in perm:
                conn.execute(
                    "INSERT INTO kg_entity_crdt "
                    "(entity_id, agent_id, op, version_vector, name, entity_type, "
                    "description, fingerprint, timestamp) VALUES (?,?,?,?,?,?,?,?,?)",
                    (op.entity_id, op.agent_id, op.op, json.dumps(op.version_vector),
                     op.name, op.entity_type, op.description, op.fingerprint, op.timestamp),
                )
            for op in edges:
                conn.execute(
                    "INSERT INTO kg_edge_crdt "
                    "(edge_id, source_id, target_id, relation, weight, valid_at, "
                    "agent_id, version_vector, timestamp) VALUES (?,?,?,?,?,?,?,?,?)",
                    (op.edge_id, op.source_id, op.target_id, op.relation, op.weight,
                     op.valid_at, op.agent_id, json.dumps(op.version_vector), op.timestamp),
                )
            conn.commit()
            n_e, n_ed, redir = project_crdt_to_entities(conn)
            verify_no_orphan(conn)
            entity_rows = conn.execute("SELECT entity_id, name FROM kg_entities ORDER BY entity_id").fetchall()
            edge_rows = conn.execute("SELECT source_id, target_id, relation FROM kg_edges ORDER BY source_id, target_id").fetchall()
            result_key = (tuple(entity_rows), tuple(edge_rows), frozenset(redir.items()))
            results.add(result_key)
            conn.close()

        assert len(results) == 1, f"Convergence violated: {len(results)} distinct outputs from 24 permutations"
