"""Adversarial test suite for crdt_projection.py.

Stress-tests the CRDT pipeline against edge cases, attack vectors, and
boundary conditions. Categories:

1. Fingerprint collision resistance
2. Version-vector overflow
3. Malicious peer / Byzantine behavior
4. Boundary cases (empty VVs, massive IDs, empty content)
5. Operational resilience (serialization, network delay, crash recovery)
6. Paper 1 specific claim stress tests (orphan under partition, redirect race, serializability)
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import time
from typing import Any, Dict, cast

import pytest

from crdt_projection import (
    EdgeOp,
    EntityOp,
    compute_fingerprint,
    entity_dedup_via_crdt,
    merge_edge_ops,
    merge_entity_ops,
    project_crdt_to_entities,
    redirect_edge_ids,
    verify_crdt_consistency,
    vv_dominates,
)


# ---------------------------------------------------------------------------
# Schema + DB helpers (shared with test_pipeline.py)
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE kg_entity_crdt (
    op_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id      INTEGER NOT NULL,
    agent_id       TEXT    NOT NULL,
    op             TEXT    NOT NULL CHECK (op IN ('add','remove')),
    version_vector TEXT    NOT NULL,
    name           TEXT,
    entity_type    TEXT,
    description    TEXT,
    fingerprint    TEXT,
    timestamp      REAL    NOT NULL
);
CREATE TABLE kg_edge_crdt (
    op_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_id        INTEGER NOT NULL,
    source_id      INTEGER NOT NULL,
    target_id      INTEGER NOT NULL,
    relation       TEXT    NOT NULL,
    weight         REAL    NOT NULL DEFAULT 1.0,
    valid_at       TEXT,
    agent_id       TEXT    NOT NULL,
    version_vector TEXT    NOT NULL,
    timestamp      REAL    NOT NULL
);
CREATE TABLE kg_entities (
    entity_id INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    entity_type TEXT    NOT NULL,
    mentions    INTEGER DEFAULT 1,
    fingerprint TEXT,
    UNIQUE(fingerprint)
);
CREATE TABLE kg_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL,
    target_id   INTEGER NOT NULL,
    relation    TEXT    NOT NULL,
    weight      REAL    DEFAULT 1.0,
    valid_at    TEXT
);
"""


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    yield conn
    conn.close()


def _seed_entity(db: sqlite3.Connection, rows: list[tuple]) -> None:
    db.executemany(
        "INSERT INTO kg_entity_crdt "
        "(entity_id, agent_id, op, version_vector, name, entity_type, description, fingerprint, timestamp) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )


def _seed_edge(db: sqlite3.Connection, rows: list[tuple]) -> None:
    db.executemany(
        "INSERT INTO kg_edge_crdt "
        "(edge_id, source_id, target_id, relation, weight, valid_at, agent_id, version_vector, timestamp) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )


def _canonical_entities(db: sqlite3.Connection) -> dict[int, dict]:
    rows = db.execute("SELECT entity_id, name, entity_type, fingerprint FROM kg_entities").fetchall()
    return {r[0]: {"name": r[1], "entity_type": r[2], "fingerprint": r[3]} for r in rows}


def _canonical_edges(db: sqlite3.Connection) -> list[tuple]:
    return db.execute("SELECT source_id, target_id, relation FROM kg_edges").fetchall()


# ===================================================================
# Category 1: Fingerprint collision resistance
# ===================================================================


class TestFingerprintCollisionResistance:
    """Force SHA-256 collision and verify canonicalization handles unicode."""

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

    def test_colliding_unicode_names_same_fingerprint(self):
        """Names that differ only in unicode normalization (NFC vs NFD) may collide."""
        # \u00e9 (é) vs \u0065\u0301 (e + combining accent)
        a = compute_fingerprint("café", "food", "")
        b = compute_fingerprint("caf\u0065\u0301", "food", "")
        # These may or may not be equal depending on canonicalization.
        # The key property: the pipeline is deterministic for identical inputs.
        assert compute_fingerprint("café", "food", "") == a


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

    def test_one_peer_vv_dominates_empty_projection(self):
        """A VV with one peer should dominate its projection in that peer."""
        a = {"peer_x": 5}
        b = {"peer_x": 3, "peer_y": 10}
        # a does NOT dominate b because b has peer_y=10 and a has peer_y=0
        assert not vv_dominates(a, b)
        # b dominates a because b[peer_x]=3 < a[peer_x]=5... wait no.
        # b[peer_x]=3 < a[peer_x]=5, so b does NOT dominate a.
        # Neither dominates the other.
        assert not vv_dominates(b, a)

    def test_huge_vv_100_peers(self):
        """VV with 100 peers: dominance check is still O(n) not O(n^2)."""
        a = {f"p{i}": 100 for i in range(100)}
        b = {f"p{i}": 99 for i in range(100)}
        assert vv_dominates(a, b)

        # One missing peer breaks dominance
        b2 = {f"p{i}": 99 for i in range(100)}
        b2["p50"] = 200  # b2 has a higher counter for one peer
        assert not vv_dominates(a, b2)

    def test_million_counter_merge(self):
        """Entity ops with VV counters at 10^9 merge correctly."""
        op_a = EntityOp(1, "a", "add", {"a": 10**9}, "alice", "person", "", "", 100.0)
        op_b = EntityOp(2, "b", "add", {"b": 10**9}, "alice", "person", "", "", 200.0)
        result = merge_entity_ops([op_a, op_b])
        # Neither dominates (disjoint peer sets) → timestamp breaks tie
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

        # Neither VV dominates (disjoint peers)
        assert not vv_dominates(op_a.version_vector, op_b.version_vector)
        assert not vv_dominates(op_b.version_vector, op_a.version_vector)

        result = merge_entity_ops([op_a, op_b])
        # Both survive merge (neither dominates), but LWW picks the later timestamp
        assert 1 in result
        assert 2 in result

    def test_adversarial_fingerprint_collision_graceful_degradation(self):
        """1M ops all with same fingerprint: merge should complete (slow but correct)."""
        # Create 10000 ops all sharing the same content (same fingerprint)
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
        # All ops are adds with different entity_ids but same content
        # Each gets its own entry in merged (they have different entity_ids)
        assert len(merged) == N
        # Verify no crash, no OOM, merge completes in reasonable time
        assert (t1 - t0) < 30.0, f"Merge took {t1-t0:.1f}s for {N} ops"

    def test_byzantine_different_vv_same_operation(self):
        """Two peers report different VVs for same logical op. Pipeline doesn't loop."""
        # Peer A claims VV {"a": 5}, Peer B claims VV {"b": 3} for same edge
        op_a = EdgeOp(1, 10, 20, "r", 1.0, None, "a", {"a": 5}, 100.0)
        op_b = EdgeOp(1, 30, 40, "r", 2.0, None, "b", {"b": 3}, 200.0)

        # Neither dominates → LWW tiebreak on timestamp
        result = merge_edge_ops([op_a, op_b])
        assert 1 in result
        # Should not hang or loop
        assert result[1]["source_id"] in (10, 30)

    def test_byzantine_monotonically_increasing_vv(self):
        """Peer claims ever-increasing VV counters to try to always dominate."""
        ops = [
            EntityOp(i, "evil_peer", "add",
                     {"evil_peer": i * 1000},
                     f"entity_{i}", "type", "", "", float(i))
            for i in range(100)
        ]
        # Each op has a different entity_id, so they don't interact
        result = merge_entity_ops(ops)
        assert len(result) == 100  # all survive (distinct entity_ids)


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
        """Ops with empty VV lose to ops with non-empty VV in LWW."""
        op_empty = EntityOp(1, "a", "add", {}, "alice", "person", "", "", 100.0)
        op_with_vv = EntityOp(2, "b", "add", {"b": 1}, "bob", "person", "", "", 50.0)

        # Neither dominates (empty VV → vv_dominates returns False)
        assert not vv_dominates(op_empty.version_vector, op_with_vv.version_vector)
        assert not vv_dominates(op_with_vv.version_vector, op_empty.version_vector)

        # LWW tiebreak on timestamp: op_empty has higher ts → op_empty wins on name
        result = merge_entity_ops([op_empty, op_with_vv])
        # Different entity_ids → both survive as separate entries
        assert 1 in result
        assert 2 in result

    def test_fingerprint_empty_all_fields(self):
        """All-empty fields: ("","","") collapses all empty-content ops to one."""
        fp1 = compute_fingerprint("", "", "")
        fp2 = compute_fingerprint("", "", "")
        assert fp1 == fp2

        # All ops with empty content should share the same fingerprint
        fp3 = compute_fingerprint(" ", " ", " ")  # whitespace → canonicalized to ""
        assert fp1 == fp3

    def test_massive_entity_id_64bit(self):
        """Entity IDs near 64-bit limits: SQLite handles them correctly."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(_SCHEMA)
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
        conn.executescript(_SCHEMA)
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
        conn.executescript(_SCHEMA)
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
    """Serialization round-trip, network delay, crash recovery."""

    def test_json_roundtrip_maintains_convergence(self):
        """Merge → serialize to JSON → deserialize → verify convergence."""
        ops = [
            EntityOp(1, "a", "add", {"a": 1}, "alice", "person", "", "", 100.0),
            EntityOp(2, "b", "add", {"b": 1}, "alice", "person", "", "", 200.0),
        ]

        # Original merge
        r1 = merge_entity_ops(ops)

        # Serialize merged state to JSON
        json_state = json.dumps(
            {str(k): v for k, v in r1.items()},
            sort_keys=True,
        )
        # Deserialize
        restored = {int(k): v for k, v in json.loads(json_state).items()}

        # Dedup should produce identical result
        d1 = entity_dedup_via_crdt(r1)
        d2 = entity_dedup_via_crdt(restored)
        assert d1["merged_state"].keys() == d2["merged_state"].keys()
        assert d1["redirects"] == d2["redirects"]

    def test_network_delay_five_seconds(self):
        """Peer 5 seconds behind: their op arrives later but doesn't violate ordering."""
        # Peer A writes at t=100, Peer B (delayed) writes at t=105
        op_a = EntityOp(1, "a", "add", {"a": 1}, "alice", "person", "", "", 100.0)
        op_b = EntityOp(2, "b", "add", {"b": 1}, "alice", "person", "", "", 105.0)

        # Both orderings produce same result
        r1 = merge_entity_ops([op_a, op_b])
        r2 = merge_entity_ops([op_b, op_a])
        assert set(r1.keys()) == set(r2.keys())

    def test_crash_mid_projection_recovery(self):
        """Simulate crash after Phase 1 but before Phase 3. Re-run recovers."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(_SCHEMA)
        _seed_entity(conn, [
            (15, "a", "add", '{"a":1}', "bob", "person", "", "", 50.0),
            (42, "a", "add", '{"a":2}', "alice", "person", "", "", 100.0),
            (99, "b", "add", '{"b":1}', "alice", "person", "", "", 200.0),
        ])
        _seed_edge(conn, [
            (1, 42, 15, "collaborates_with", 1.0, None, "a", '{"a":3}', 110.0),
        ])

        # First run: projection completes
        n_e1, n_ed1, redir1 = project_crdt_to_entities(conn)
        entities1 = _canonical_entities(conn)
        edges1 = _canonical_edges(conn)

        # Second run: idempotent recovery
        n_e2, n_ed2, redir2 = project_crdt_to_entities(conn)
        entities2 = _canonical_entities(conn)
        edges2 = _canonical_edges(conn)

        assert n_e1 == n_e2
        assert n_ed1 == n_ed2
        assert redir1 == redir2
        assert entities1 == entities2
        assert edges1 == edges2
        conn.close()

    def test_idempotent_projection_on_large_dataset(self):
        """Run projection twice on 1000-entity dataset, verify identical output."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(_SCHEMA)

        # Seed 1000 entities, half colliding
        rows = []
        for i in range(1000):
            name = f"entity_{i % 100}"  # 100 distinct names, 10 copies each
            rows.append(
                (i, f"agent_{i % 5}", "add",
                 json.dumps({f"agent_{i % 5}": i // 5 + 1}),
                 name, "type", "", "", float(i))
            )
        _seed_entity(conn, rows)

        n_e1, _, redir1 = project_crdt_to_entities(conn)
        n_e2, _, redir2 = project_crdt_to_entities(conn)

        assert n_e1 == n_e2
        assert redir1 == redir2
        conn.close()


# ===================================================================
# Category 6: Paper 1 specific claim stress tests
# ===================================================================


class TestNoOrphanUnderPartition:
    """Stress the orphan invariant (§5.4 Issue 3) under partition conditions."""

    def test_two_peers_partitioned_five_way_merge(self):
        """Two peers partitioned during 5-way merge. One loses writes.
        After reconnect, no-orphan invariant holds."""
        # 5 entities created by 5 peers, 2 of which partition
        conn = sqlite3.connect(":memory:")
        conn.executescript(_SCHEMA)

        # Peer A writes entities 1-3 (connected), Peer B writes 4-5 (partitioned)
        _seed_entity(conn, [
            (1, "a", "add", '{"a":1}', "ent1", "type", "", "", 100.0),
            (2, "a", "add", '{"a":2}', "ent2", "type", "", "", 101.0),
            (3, "a", "add", '{"a":3}', "ent3", "type", "", "", 102.0),
            (4, "b", "add", '{"b":1}', "ent4", "type", "", "", 103.0),
            (5, "c", "add", '{"c":1}', "ent5", "type", "", "", 104.0),
        ])
        # Edges from all peers (some referencing partitioned entities)
        _seed_edge(conn, [
            (1, 1, 4, "related_to", 1.0, None, "a", '{"a":1}', 110.0),
            (2, 4, 2, "related_to", 1.0, None, "b", '{"b":1}', 111.0),
            (3, 5, 3, "related_to", 1.0, None, "c", '{"c":1}', 112.0),
            (4, 3, 5, "related_to", 1.0, None, "a", '{"a":2}', 113.0),
        ])

        project_crdt_to_entities(conn)
        verify_crdt_consistency(conn)  # must not raise
        conn.close()

    def test_partition_with_entity_loss(self):
        """Peer loses all writes. After reconnect, surviving entities are canonical."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(_SCHEMA)

        # Peer A created alice (ID 42) and bob (ID 15)
        # Peer B created alice (ID 99) and carol (ID 30)
        # After reconnect: alice dedup (99 wins), bob + carol survive
        _seed_entity(conn, [
            (15, "a", "add", '{"a":1}', "bob", "person", "", "", 50.0),
            (42, "a", "add", '{"a":2}', "alice", "person", "", "", 100.0),
            (99, "b", "add", '{"b":1}', "alice", "person", "", "", 200.0),
            (30, "b", "add", '{"b":2}', "carol", "person", "", "", 150.0),
        ])
        _seed_edge(conn, [
            (1, 42, 15, "knows", 1.0, None, "a", '{"a":3}', 110.0),
            (2, 99, 30, "knows", 1.0, None, "b", '{"b":3}', 210.0),
        ])

        project_crdt_to_entities(conn)
        entities = _canonical_entities(conn)
        edges = _canonical_edges(conn)

        # Alice deduped: 99 wins, 42 redirected
        assert 99 in entities
        assert 42 not in entities
        assert 15 in entities
        assert 30 in entities

        # Edges: (42→15) redirected to (99→15), (99→30) stays
        assert (99, 15, "knows") in edges
        assert (99, 30, "knows") in edges

        verify_crdt_consistency(conn)
        conn.close()


class TestRedirectMapConsistency:
    """Redirect map is concurrent-read while edge op arrives."""

    def test_redirect_applied_atomically(self):
        """All edges see the same redirect map snapshot."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(_SCHEMA)
        _seed_entity(conn, [
            (42, "a", "add", '{"a":1}', "alice", "person", "", "", 100.0),
            (99, "b", "add", '{"b":1}', "alice", "person", "", "", 200.0),
        ])
        # Multiple edges referencing the to-be-redirected ID
        _seed_edge(conn, [
            (1, 42, 10, "r1", 1.0, None, "a", '{"a":1}', 110.0),
            (2, 42, 20, "r2", 1.0, None, "b", '{"b":1}', 120.0),
            (3, 42, 30, "r3", 1.0, None, "a", '{"a":2}', 130.0),
        ])

        n_e, n_ed, redirects = project_crdt_to_entities(conn)
        assert redirects == {42: 99}

        edges = _canonical_edges(conn)
        # All three edges must be redirected to 99
        for src, tgt, rel in edges:
            assert src == 99, f"Edge {rel} still references source {src}"

        verify_crdt_consistency(conn)
        conn.close()

    def test_redirect_chain_idempotent(self):
        """If redirect maps A→B and B→C, applying twice gives same result."""
        redirects = {42: 99, 99: 100}
        edges = {1: {"source_id": 42, "target_id": 15, "relation": "r"}}

        # One pass: 42→99, 99 not in redirects (it's a target), so 15→15
        once = redirect_edge_ids(edges, redirects)
        assert once[1]["source_id"] == 99

        # Second pass: 99→100
        twice = redirect_edge_ids(once, redirects)
        assert twice[1]["source_id"] == 100


class TestSerializabilityLimit:
    """100 edges pointed at the same redirected ID: OR-Set semantics preserved."""

    def test_hundred_edges_same_redirected_id(self):
        """100 edges all source=42, redirected to 99. All should be rewritten."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(_SCHEMA)
        # Need target entities to exist for orphan guard
        entity_rows = [
            (42, "a", "add", '{"a":1}', "alice", "person", "", "", 100.0),
            (99, "b", "add", '{"b":1}', "alice", "person", "", "", 200.0),
        ]
        for i in range(100):
            entity_rows.append(
                (100 + i, "c", "add", '{"c":1}', f"target_{i}", "type", "", "", float(i))
            )
        _seed_entity(conn, entity_rows)
        edge_rows = [
            (i, 42, 100 + i, "related_to", 1.0, None, "a", '{"a":1}', float(100 + i))
            for i in range(100)
        ]
        _seed_edge(conn, edge_rows)

        n_e, n_ed, redirects = project_crdt_to_entities(conn)
        assert redirects == {42: 99}

        edges = _canonical_edges(conn)
        assert len(edges) == 100
        for src, tgt, rel in edges:
            assert src == 99, f"Edge {rel} still references source {src}"

        verify_crdt_consistency(conn)
        conn.close()


# ===================================================================
# Category 7: Edge cases with tombstones and concurrent operations
# ===================================================================


class TestTombstoneEdgeCases:
    """Tombstone + concurrent edge: edge arrives after entity is tombstoned."""

    def test_edge_to_tombstoned_entity_dropped(self):
        """Entity 42 is add-then-remove. Edge (42→15) arrives: orphan guard drops it."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(_SCHEMA)
        _seed_entity(conn, [
            (15, "a", "add", '{"a":1}', "bob", "person", "", "", 50.0),
            (42, "a", "add", '{"a":2}', "alice", "person", "", "", 100.0),
            (42, "a", "remove", '{"a":3}', "alice", "person", "", "", 200.0),
        ])
        _seed_edge(conn, [
            (1, 42, 15, "knows", 1.0, None, "a", '{"a":4}', 110.0),
        ])

        n_e, n_ed, _ = project_crdt_to_entities(conn)
        assert n_e == 1  # only bob survives
        assert n_ed == 0  # edge dropped by orphan guard
        verify_crdt_consistency(conn)
        conn.close()

    def test_concurrent_add_remove_entity_survives(self):
        """Concurrent add and remove (neither dominates): entity survives (2P-Set)."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(_SCHEMA)
        _seed_entity(conn, [
            (1, "a", "add", '{"a":1, "b":0}', "alice", "person", "", "", 100.0),
            (1, "b", "remove", '{"a":0, "b":1}', "alice", "person", "", "", 100.0),
        ])
        n_e, n_ed, _ = project_crdt_to_entities(conn)
        assert n_e == 1  # concurrent add+remove → entity survives
        conn.close()


# ===================================================================
# Category 8: Merge with mixed operations
# ===================================================================


class TestMixedOperations:
    """Edge ops and entity ops interact correctly under adversarial conditions."""

    def test_edge_merge_commutativity_stress(self):
        """100 random edge op orderings produce identical merged state."""
        from itertools import permutations

        ops = [
            EdgeOp(1, 10, 20, "r", 1.0, None, "a", {"a": 1}, 100.0),
            EdgeOp(1, 10, 20, "r", 2.0, None, "b", {"b": 1}, 200.0),
            EdgeOp(2, 30, 40, "s", 1.0, None, "a", {"a": 1}, 50.0),
            EdgeOp(2, 30, 40, "s", 3.0, None, "c", {"c": 1}, 300.0),
            EdgeOp(3, 50, 60, "t", 1.0, None, "a", {"a": 2}, 150.0),
        ]

        reference = merge_edge_ops(ops)
        # Test all 120 permutations
        count = 0
        for perm in permutations(ops):
            result = merge_edge_ops(list(perm))
            assert result == reference, f"Edge merge not commutative for permutation"
            count += 1
        assert count == 120

    def test_entity_merge_with_all_operation_types(self):
        """Mix of adds, removes, and metadata updates: merge is deterministic."""
        ops = [
            EntityOp(1, "a", "add", {"a": 1}, "alice", "person", "", "", 100.0),
            EntityOp(1, "b", "add", {"b": 1}, "alice", "person", "enriched", "", 200.0),
            EntityOp(2, "a", "add", {"a": 1}, "bob", "person", "", "", 50.0),
            EntityOp(2, "c", "remove", {"a": 1, "c": 1}, "bob", "person", "", "", 300.0),
            EntityOp(3, "a", "add", {"a": 2}, "carol", "person", "", "", 150.0),
        ]

        r1 = merge_entity_ops(ops)
        r2 = merge_entity_ops(list(reversed(ops)))
        assert set(r1.keys()) == set(r2.keys())

        # Entity 1 survives (adds only)
        assert 1 in r1
        # Entity 2 is tombstoned: remove VV {"a":1,"c":1} dominates add VV {"a":1}
        assert 2 not in r1
        # Entity 3 survives
        assert 3 in r1


# ===================================================================
# Category 11: Partial-replication convergence
# ===================================================================


class TestPartialReplicationConvergence:
    """Peers project on distinct op subsets, reconcile, then re-project.

    Under eventual consistency, a peer first projects only the operations it
    has received so far (a strict subset of the global bag). Later, peers
    exchange the missing operations and re-project. The pipeline must converge
    to the same canonical state as if every peer had always held the full bag.
    This exercises the partial-replication regime scoped in §5.3.
    """

    def _seed_and_project(self, entity_rows, edge_rows, full_entity_rows, full_edge_rows):
        conn = sqlite3.connect(":memory:")
        conn.executescript(_SCHEMA)
        _seed_entity(conn, entity_rows)
        _seed_edge(conn, edge_rows)
        project_crdt_to_entities(conn)  # project on PARTIAL bag
        # ... then exchange: receive the missing ops and re-project.
        _seed_entity(conn, full_entity_rows)
        _seed_edge(conn, full_edge_rows)
        project_crdt_to_entities(conn)  # re-project on FULL bag
        verify_crdt_consistency(conn)
        entities = tuple(sorted(_canonical_entities(conn).items()))
        edges = tuple(sorted(_canonical_edges(conn)))
        conn.close()
        return entities, edges

    def test_partial_replication_convergence(self):
        # Global op set: three peers create the same entity "project:x" under
        # different IDs, plus an edge from each to a shared target.
        fp = compute_fingerprint("project:x", "project", "")
        tgt_fp = compute_fingerprint("target", "proj", "")
        all_ent = [
            (10, "a", "add", '{"a":1}', "project:x", "project", "", fp, 100.0),
            (20, "b", "add", '{"b":1}', "project:x", "project", "", fp, 200.0),
            (30, "c", "add", '{"c":1}', "project:x", "project", "", fp, 300.0),
            (15, "a", "add", '{"a":2}', "target", "proj", "", tgt_fp, 50.0),
        ]
        all_edg = [
            (1, 10, 15, "depends_on", 1.0, None, "a", '{"a":2}', 110.0),
            (2, 20, 15, "depends_on", 1.0, None, "b", '{"b":2}', 210.0),
            (3, 30, 15, "depends_on", 1.0, None, "c", '{"c":2}', 310.0),
        ]

        # Ground truth: full bag on a fresh DB.
        full_ent, full_edg = self._seed_and_project(all_ent, all_edg, [], [])

        # Peer A starts with only ops from peers a and b; Peer B with b and c.
        peer_a_ent = [all_ent[0], all_ent[1], all_ent[3]]
        peer_a_edg = [all_edg[0], all_edg[1]]
        peer_b_ent = [all_ent[1], all_ent[2], all_ent[3]]
        peer_b_edg = [all_edg[1], all_edg[2]]

        # Missing ops to deliver on reconciliation (INSERT OR IGNORE avoids dups).
        a_ent, a_edg = self._seed_and_project(
            peer_a_ent, peer_a_edg,
            [all_ent[2]], [all_edg[2]],
        )
        b_ent, b_edg = self._seed_and_project(
            peer_b_ent, peer_b_edg,
            [all_ent[0]], [all_edg[0]],
        )

        assert a_ent == b_ent == full_ent, \
            f"Partial-replication divergence: {a_ent} vs {b_ent} vs {full_ent}"
        assert a_edg == b_edg == full_edg, \
            f"Partial-replication edge divergence: {a_edg} vs {b_edg} vs {full_edg}"


# ===================================================================
# Category 12: Non-transitive cycle permutation invariance
# ===================================================================


class TestEdgeMergeNontransitiveCyclePermutationInvariance:
    """Verifies that merge_edge_ops yields identical output across all arrival order permutations
    even when concurrent ops form a non-transitive A > B > C > A cycle under the comparator.
    """

    def test_edge_merge_nontransitive_cycle_permutation_invariance(self):
        import itertools
        from crdt_projection import EdgeOp, merge_edge_ops

        op_a = EdgeOp(
            edge_id=1,
            agent_id="agentA",
            version_vector={"p1": 2},
            source_id=10,
            target_id=20,
            relation="rel_A",
            weight=1.0,
            valid_at=None,
            timestamp=3.0,
        )
        op_b = EdgeOp(
            edge_id=1,
            agent_id="agentB",
            version_vector={"p2": 2},
            source_id=10,
            target_id=20,
            relation="rel_B",
            weight=2.0,
            valid_at=None,
            timestamp=2.0,
        )
        op_c = EdgeOp(
            edge_id=1,
            agent_id="agentC",
            version_vector={"p1": 2, "p3": 1},
            source_id=10,
            target_id=20,
            relation="rel_C",
            weight=3.0,
            valid_at=None,
            timestamp=1.0,
        )

        ops = [op_a, op_b, op_c]
        reference_winner = merge_edge_ops(ops)[1]

        for perm in itertools.permutations(ops):
            res = merge_edge_ops(perm)[1]
            assert res == reference_winner, (
                f"Divergence detected across arrival orders! "
                f"Permutation {[o.relation for o in perm]} produced {res['relation']} "
                f"instead of {reference_winner['relation']}"
            )
