"""Test suite for crdt_projection.py — standalone three-phase CRDT pipeline.

Tests are self-contained: each one builds its own in-memory SQLite DB from a
schema string, seeds CRDT ops, calls project_crdt_to_entities, then asserts
on the canonical state and redirect map.

All tests use the same fallback schema / helpers so cases stay readable.
"""

from __future__ import annotations

import sqlite3
import pytest
from typing import Any, Dict, cast


from crdt_projection import (
    EntityOp,
    EdgeOp,
    compute_fingerprint,
    merge_entity_ops,
    entity_dedup_via_crdt,
    redirect_edge_ids,
    merge_edge_ops,
    project_crdt_to_entities,
    verify_crdt_consistency,
)


# ---------------------------------------------------------------------------
# Schema + DB helpers
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
    """In-memory SQLite DB with the full schema. Closed after each test."""
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


# ---------------------------------------------------------------------------
# Scenario 1: basic concurrent creation (Section 3.2 / Section 4.5),
# ---------------------------------------------------------------------------


class TestScenario1BasicConcurrentCreation:
    def test_single_alice_collision(self, db):
        """Two agents create 'alice' under IDs 42 and 99."""
        _seed_entity(
            db,
            [
                (15, "agent_a", "add", '{"agent_a":1}', "bob", "person", "", "", 50.0),
                (23, "agent_b", "add", '{"agent_b":1}', "charlie", "person", "", "", 150.0),
                (42, "agent_a", "add", '{"agent_a":2}', "alice", "person", "", "", 100.0),
                (99, "agent_b", "add", '{"agent_b":2}', "alice", "person", "", "", 200.0),
            ],
        )
        _seed_edge(
            db,
            [
                (
                    1,
                    42,
                    15,
                    "collaborates_with",
                    1.0,
                    None,
                    "agent_a",
                    '{"agent_a":3}',
                    110.0,
                ),
                (
                    2,
                    99,
                    23,
                    "collaborates_with",
                    1.0,
                    None,
                    "agent_b",
                    '{"agent_b":3}',
                    210.0,
                ),
            ],
        )

        n_e, n_ed, redirects = project_crdt_to_entities(db)

        # Exactly one canonical alice
        entities = _canonical_entities(db)
        alice_id = 99  # winner = max(42, 99)
        assert alice_id in entities, f"expected entity {alice_id}, got {list(entities)}"
        assert entities[alice_id]["name"] == "alice"
        assert entities[alice_id]["entity_type"] == "person"
        assert n_e == 3  # bob, charlie, alice(winner)

        # Bob (15) and charlie (23) preserved
        assert 15 in entities
        assert 23 in entities

        # Redirect map
        assert redirects == {42: 99}

        # Both edges rewritten to point to alice(99)
        edges = _canonical_edges(db)
        assert n_ed == 2
        assert (99, 15, "collaborates_with") in edges
        assert (99, 23, "collaborates_with") in edges

        # No edge still references 42
        for src, tgt, _rel in edges:
            assert src != 42
            assert tgt != 42

        # No-orphan invariant
        verify_crdt_consistency(db)


# ---------------------------------------------------------------------------
# Scenario 2: three-way concurrent creation
# ---------------------------------------------------------------------------


class TestScenario2ThreeWayCreation:
    def test_three_agents_same_entity(self, db):
        """Agents A, B, C each create 'project:x' under IDs 10, 20, 30."""
        # Entity ops for project candidates + their edge targets (real entities).
        _seed_entity(
            db,
            [
                (10, "agent_a", "add", '{"agent_a":1}', "project:x", "project", "", "", 100.0),
                (20, "agent_b", "add", '{"agent_b":1}', "project:x", "project", "", "", 200.0),
                (30, "agent_c", "add", '{"agent_c":1}', "project:x", "project", "", "", 300.0),
                (15, "agent_a", "add", '{"agent_a":2}', "target_1", "project", "", "", 50.0),
                (23, "agent_b", "add", '{"agent_b":2}', "target_2", "project", "", "", 150.0),
                (25, "agent_c", "add", '{"agent_c":2}', "target_3", "project", "", "", 250.0),
            ],
        )
        _seed_edge(
            db,
            [
                (1, 10, 15, "depends_on", 1.0, None, "agent_a", '{"agent_a":2}', 110.0),
                (2, 20, 23, "depends_on", 1.0, None, "agent_b", '{"agent_b":2}', 210.0),
                (3, 30, 25, "depends_on", 1.0, None, "agent_c", '{"agent_c":2}', 310.0),
            ],
        )

        n_e, n_ed, redirects = project_crdt_to_entities(db)

        entities = _canonical_entities(db)
        assert 30 in entities  # winner = max(10, 20, 30)
        assert 10 not in entities
        assert 20 not in entities

        assert redirects == {10: 30, 20: 30}
        assert n_e == 4  # winner(30) + 3 edge targets

        edges = _canonical_edges(db)
        assert n_ed == 3
        # All edges must reference 30
        for src, tgt, _rel in edges:
            assert src == 30

        verify_crdt_consistency(db)


# ---------------------------------------------------------------------------
# Scenario 3: post-merge edge addition referencing a merged-away ID
# ---------------------------------------------------------------------------


class TestScenario3PostMergeEdgeAddition:
    def test_edge_to_merged_away_entity_is_rewritten(self, db):
        """After scenario-1 merge, agent D adds edge (42 → 77)."""
        # Pre-seed scenario-1
        _seed_entity(
            db,
            [
                (15, "agent_a", "add", '{"agent_a":1}', "bob", "person", "", "", 50.0),
                (23, "agent_b", "add", '{"agent_b":1}', "charlie", "person", "", "", 150.0),
                (42, "agent_a", "add", '{"agent_a":2}', "alice", "person", "", "", 100.0),
                (99, "agent_b", "add", '{"agent_b":2}', "alice", "person", "", "", 200.0),
                # Agent D's new entity (edge target)
                (77, "agent_d", "add", '{"agent_d":1}', "eve", "person", "", "", 500.0),
            ],
        )
        # Edge ops from A and B — both reference old IDs.
        _seed_edge(
            db,
            [
                (
                    1,
                    42,
                    15,
                    "collaborates_with",
                    1.0,
                    None,
                    "agent_a",
                    '{"agent_a":3}',
                    110.0,
                ),
                (
                    2,
                    99,
                    23,
                    "collaborates_with",
                    1.0,
                    None,
                    "agent_b",
                    '{"agent_b":3}',
                    210.0,
                ),
                # Agent D adds an edge to 42 (which will be redirected to 99)
                (3, 42, 77, "knows", 1.0, None, "agent_d", '{"agent_d":1}', 500.0),
            ],
        )

        n_e, n_ed, redirects = project_crdt_to_entities(db)

        # Entity 77 must also be present (separate real entity)
        entities = _canonical_entities(db)
        assert 77 in entities, "entity 77 should survive independently"

        # Edge (42 → 77) must be rewritten to (99 → 77)
        edges = _canonical_edges(db)
        assert (99, 77, "knows") in edges, f"expected (99, 77, 'knows') in {edges}"
        # No edge should still reference source 42
        assert all(src != 42 for src, _, _ in edges)

        verify_crdt_consistency(db)


# ---------------------------------------------------------------------------
# Pure Phase 3: redirect_edge_ids unit tests
# ---------------------------------------------------------------------------


class TestRedirectEdgeIds:
    def test_no_redirects_passthrough(self):
        edges = {1: {"source_id": 10, "target_id": 20, "relation": "r"}}
        result = redirect_edge_ids(edges, {})
        assert result == edges

    def test_single_source_rewrite(self):
        edges = {1: {"source_id": 42, "target_id": 15, "relation": "r"}}
        result = redirect_edge_ids(edges, {42: 99})
        assert result[1]["source_id"] == 99
        assert result[1]["target_id"] == 15

    def test_single_target_rewrite(self):
        edges = {1: {"source_id": 10, "target_id": 77, "relation": "r"}}
        result = redirect_edge_ids(edges, {77: 99})
        assert result[1]["source_id"] == 10
        assert result[1]["target_id"] == 99

    def test_both_endpoints_rewrite(self):
        edges = {1: {"source_id": 42, "target_id": 15, "relation": "r"}}
        result = redirect_edge_ids(edges, {42: 99, 15: 88})
        assert result[1]["source_id"] == 99
        assert result[1]["target_id"] == 88

    def test_idempotent_on_winner(self):
        """Applying redirect twice yields same result as applying once."""
        redirects = {42: 99}
        edges = {1: {"source_id": 99, "target_id": 20, "relation": "r"}}
        once = redirect_edge_ids(edges, redirects)
        twice = redirect_edge_ids(once, redirects)
        assert once == twice

    def test_multiple_edges_multiple_redirects(self):
        edges = {
            1: {"source_id": 10, "target_id": 20, "relation": "r1"},
            2: {"source_id": 20, "target_id": 30, "relation": "r2"},
        }
        result = redirect_edge_ids(edges, {10: 99, 20: 88})
        assert result[1]["source_id"] == 99
        assert result[1]["target_id"] == 88
        assert result[2]["source_id"] == 88
        assert result[2]["target_id"] == 30


# ---------------------------------------------------------------------------
# Pure Phase 2: entity_dedup_via_crdt unit tests
# ---------------------------------------------------------------------------


class TestEntityDedup:
    def _merged(
        self,
        entries: dict[int, dict],
    ) -> dict[int, dict]:
        """Helper: build a minimal merged_state dict."""
        return entries

    def test_single_entry_no_collision(self):
        state = self._merged({42: {"name": "alice", "entity_type": "person"}})
        result = entity_dedup_via_crdt(state)
        assert result["merged_state"] == {42: state[42]}
        assert result["redirects"] == {}

    def test_two_entries_same_key_picks_max_id(self):
        state = self._merged(
            {
                42: {"name": "alice", "entity_type": "person"},
                99: {"name": "alice", "entity_type": "person"},
            }
        )
        result = entity_dedup_via_crdt(state)
        assert list(result["merged_state"]) == [99]
        assert result["redirects"] == {42: 99}

    def test_three_entries_same_key(self):
        state = self._merged(
            {
                10: {"name": "p", "entity_type": "project"},
                20: {"name": "p", "entity_type": "project"},
                30: {"name": "p", "entity_type": "project"},
            }
        )
        result = entity_dedup_via_crdt(state)
        assert list(result["merged_state"]) == [30]
        assert result["redirects"] == {10: 30, 20: 30}

    def test_distinct_keys_no_dedup(self):
        state = self._merged(
            {
                1: {"name": "alice", "entity_type": "person"},
                2: {"name": "bob", "entity_type": "person"},
            }
        )
        result = entity_dedup_via_crdt(state)
        assert set(result["merged_state"]) == {1, 2}
        assert result["redirects"] == {}

    def test_mixed_groups(self):
        """Two collision groups interleaved with singletons."""
        state = self._merged(
            {
                1: {"name": "alice", "entity_type": "person"},
                2: {"name": "bob", "entity_type": "person"},
                3: {"name": "alice", "entity_type": "person"},
                4: {"name": "carol", "entity_type": "person"},
            }
        )
        result = entity_dedup_via_crdt(state)
        assert set(result["merged_state"]) == {2, 3, 4}
        assert result["redirects"] == {1: 3}


# ---------------------------------------------------------------------------
# Pure Phase 1: merge_entity_ops unit tests
# ---------------------------------------------------------------------------


class TestMergeEntityOps:
    def _op(self, **kw):
        defaults: Dict[str, Any] = {
            "entity_id": 1,
            "agent_id": "a",
            "op": "add",
            "version_vector": {},
            "name": "",
            "entity_type": "",
            "description": "",
            "timestamp": 0.0,
        }
        defaults.update(kw)
        return EntityOp(
            entity_id=int(defaults["entity_id"]),
            agent_id=str(defaults["agent_id"]),
            op=str(defaults["op"]),
            version_vector=cast(Dict[str, int], defaults.get("version_vector") or {}),
            name=str(defaults.get("name", "")),
            entity_type=str(defaults.get("entity_type", "")),
            description=str(defaults.get("description", "")),
            timestamp=float(defaults.get("timestamp", 0.0)),
        )

    def test_add_only_survives(self):
        ops = [
            self._op(
                entity_id=1,
                name="alice",
                entity_type="person",
                version_vector={"a": 1},
                timestamp=100.0,
            )
        ]
        result = merge_entity_ops(ops)
        assert 1 in result
        assert result[1]["name"] == "alice"

    def test_remove_dominates_add_tombstones(self):
        add = self._op(entity_id=1, op="add", version_vector={"a": 1}, timestamp=100.0)
        rem = self._op(
            entity_id=1, op="remove", version_vector={"a": 2}, timestamp=200.0
        )
        result = merge_entity_ops([add, rem])
        assert 1 not in result

    def test_concurrent_add_remove_add_wins(self):
        """2P-Set: concurrent add and remove → entity survives."""
        add = self._op(
            entity_id=1, op="add", version_vector={"a": 1, "b": 0}, timestamp=100.0
        )
        rem = self._op(
            entity_id=1, op="remove", version_vector={"a": 0, "b": 1}, timestamp=100.0
        )
        result = merge_entity_ops([add, rem])
        assert 1 in result

    def test_lww_dominance_picks_higher_clock(self):
        """Higher-clocked op wins via vv_dominates partial order."""
        op_a = self._op(
            entity_id=1,
            name="alice",
            entity_type="person",
            version_vector={"a": 1},
            timestamp=100.0,
        )
        op_b = self._op(
            entity_id=1,
            name="alicia",
            entity_type="person",
            version_vector={"a": 2},
            timestamp=50.0,
        )
        result = merge_entity_ops([op_a, op_b])
        # vv_dominates: op_b {"a":2} dominates op_a {"a":1} → wins
        assert result[1]["name"] == "alicia"

    def test_lww_tiebreaker_timestamp(self):
        """Same VV sum, higher timestamp wins."""
        op_a = self._op(
            entity_id=1,
            name="alice",
            entity_type="person",
            version_vector={"a": 1},
            timestamp=100.0,
        )
        op_b = self._op(
            entity_id=1,
            name="alicia",
            entity_type="person",
            version_vector={"a": 1},
            timestamp=200.0,
        )
        result = merge_entity_ops([op_a, op_b])
        assert result[1]["name"] == "alicia"

    def test_lww_tiebreaker_agent_id(self):
        """Same VV sum + timestamp: lexicographically lower agent_id wins."""
        op_b = self._op(
            entity_id=1,
            name="alicia",
            entity_type="person",
            version_vector={"a": 1},
            timestamp=100.0,
            agent_id="b_peer",
        )
        op_a = self._op(
            entity_id=1,
            name="alice",
            entity_type="person",
            version_vector={"a": 1},
            timestamp=100.0,
            agent_id="a_peer",
        )
        result = merge_entity_ops([op_a, op_b])
        assert result[1]["name"] == "alice"

    def test_empty_ops_returns_empty(self):
        assert merge_entity_ops([]) == {}


# ---------------------------------------------------------------------------
# Empty-table / edge-only edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_entity_log(self, db):
        """No entity ops → no canonical entities → dangling edges are dropped
        by the orphan guard (both endpoints reference non-existent entities)."""
        _seed_edge(
            db,
            [
                (1, 42, 15, "r", 1.0, None, "a", '{"a":1}', 100.0),
            ],
        )
        n_e, n_ed, _ = project_crdt_to_entities(db)
        assert n_e == 0
        assert n_ed == 0  # orphan guard drops edges with non-existent endpoints

    def test_empty_edge_log(self, db):
        """Entity-only DB: projection writes entities, no edges."""
        _seed_entity(
            db,
            [
                (1, "a", "add", '{"a":1}', "alice", "person", "", "", 100.0),
            ],
        )
        n_e, n_ed, _ = project_crdt_to_entities(db)
        assert n_e == 1
        assert n_ed == 0

    def test_full_pipeline_idempotent(self, db):
        """Running the pipeline twice on the same logs produces identical state."""
        _seed_entity(
            db,
            [
                (15, "agent_a", "add", '{"agent_a":1}', "bob", "person", "", "", 50.0),
                (42, "agent_a", "add", '{"agent_a":2}', "alice", "person", "", "", 100.0),
                (99, "agent_b", "add", '{"agent_b":2}', "alice", "person", "", "", 200.0),
            ],
        )
        _seed_edge(
            db,
            [
                (
                    1,
                    42,
                    15,
                    "collaborates_with",
                    1.0,
                    None,
                    "agent_a",
                    '{"agent_a":3}',
                    110.0,
                ),
            ],
        )

        project_crdt_to_entities(db)
        entities_1 = _canonical_entities(db)
        edges_1 = _canonical_edges(db)

        # Re-seed same ops + run again (logs are append-only, so we add again
        # simulating a fresh sync).  In production the logs are the logs;
        # here we just re-run to verify INSERT OR REPLACE idempotence.
        project_crdt_to_entities(db)
        entities_2 = _canonical_entities(db)
        edges_2 = _canonical_edges(db)

        assert entities_1 == entities_2
        assert edges_1 == edges_2


# ---------------------------------------------------------------------------
# Determinism: same inputs → same outputs regardless of operation order
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_merge_result_order_independent(self):
        ops = [
            EntityOp(1, "a", "add", {"a": 1}, "alice", "person", "", fingerprint="", timestamp=100.0),
            EntityOp(1, "b", "add", {"b": 1}, "alice", "person", "", fingerprint="", timestamp=200.0),
        ]
        r1 = merge_entity_ops(ops)
        r2 = merge_entity_ops(reversed(ops))
        assert r1 == r2

    def test_dedup_order_independent(self):
        state = {
            42: {"name": "alice", "entity_type": "person"},
            99: {"name": "alice", "entity_type": "person"},
        }
        r1 = entity_dedup_via_crdt(state)
        r2 = entity_dedup_via_crdt({99: state[99], 42: state[42]})
        assert r1 == r2


# ---------------------------------------------------------------------------
# Phase 3 precondition: no edge references a tombstoned entity
# ---------------------------------------------------------------------------


class TestPreconditionEdgeTombstone:
    """Theorem 1 assumes no edge references an entity tombstoned in Phase 1.

    These tests exercise the three states of that precondition:
    1. Violated → verify_crdt_consistency raises.
    2. Met → no violation.
    3. Violated pre-conditions, but caller cleans up orphan edge before
       projection → no violation (expected production pattern).
    """

    def test_tombstoned_entity_with_edge_dropped_by_guard(self, db):
        """Entity 42 is tombstoned; edge (42→15) references a non-existent
        entity (42 was removed in Phase 1, never in canonical_ids).

        The orphan guard (C7 fix) drops this edge before it reaches
        kg_edges, so verify_crdt_consistency passes. This verifies that
        dangling edges caused by tombstoned entities are prevented at
        write time rather than detected after the fact.
        """
        _seed_entity(
            db,
            [
                (15, "agent_a", "add", '{"agent_a":1}', "bob", "person", "", "", 50.0),
                (42, "agent_a", "add", '{"agent_a":2}', "alice", "person", "", "", 100.0),
                (42, "agent_a", "remove", '{"agent_a":3}', "alice", "person", "", "", 200.0),
            ],
        )
        _seed_edge(
            db,
            [
                (
                    1,
                    42,
                    15,
                    "collaborates_with",
                    1.0,
                    None,
                    "agent_a",
                    '{"agent_a":4}',
                    110.0,
                ),
            ],
        )
        n_e, n_ed, redirects = project_crdt_to_entities(db)
        assert n_e == 1  # only bob survives (alice tombstoned)
        assert n_ed == 0  # orphan guard drops edge referencing tombstoned entity 42
        verify_crdt_consistency(db)  # must not raise — no orphans written

    def test_surviving_entity_with_edge_satisfies_precondition(self, db):
        """Entity 42 survives Phase 1; edge (42→15) projects cleanly."""
        _seed_entity(
            db,
            [
                (15, "agent_a", "add", '{"agent_a":1}', "bob", "person", "", "", 50.0),
                (42, "agent_a", "add", '{"agent_a":2}', "alice", "person", "", "", 100.0),
            ],
        )
        _seed_edge(
            db,
            [
                (
                    1,
                    42,
                    15,
                    "collaborates_with",
                    1.0,
                    None,
                    "agent_a",
                    '{"agent_a":3}',
                    110.0,
                ),
            ],
        )
        project_crdt_to_entities(db)
        verify_crdt_consistency(db)  # must not raise

    def test_caller_cleanup_orphan_edge_before_projection(self, db):
        """Caller removes the orphan edge from the CRDT log before projection.

        This is the expected production pattern: when an edge references a
        tombstoned entity, the caller (or a preceding cleanup step) removes
        the edge from kg_edge_crdt before running the pipeline so Phase 3
        never sees the offending reference.
        """
        _seed_entity(
            db,
            [
                (15, "agent_a", "add", '{"agent_a":1}', "bob", "person", "", "", 50.0),
                (42, "agent_a", "add", '{"agent_a":2}', "alice", "person", "", "", 100.0),
                (42, "agent_a", "remove", '{"agent_a":3}', "alice", "person", "", "", 200.0),
            ],
        )
        _seed_edge(
            db,
            [
                (
                    1,
                    42,
                    15,
                    "collaborates_with",
                    1.0,
                    None,
                    "agent_a",
                    '{"agent_a":4}',
                    110.0,
                ),
            ],
        )
        # Caller-side cleanup: remove the orphan edge before projection.
        db.execute("DELETE FROM kg_edge_crdt WHERE edge_id = 1")
        db.commit()
        project_crdt_to_entities(db)
        verify_crdt_consistency(db)  # must not raise


# ---------------------------------------------------------------------------
# Homonym disambiguation: Case 3 — different descriptions → coexist
# ---------------------------------------------------------------------------


class TestHomonymDisambiguation:
    def test_two_alices_different_descriptions_coexist(self, db):
        """Alice the lawyer and Alice the chef → different fingerprints → both survive."""
        _seed_entity(
            db,
            [
                (42, "agent_a", "add", '{"agent_a":1}', "alice", "person",
                 "corporate lawyer at Skadden", "", 100.0),
                (99, "agent_b", "add", '{"agent_b":1}', "alice", "person",
                 "executive chef at Le Bernardin", "", 200.0),
            ],
        )
        n_e, n_ed, redirects = project_crdt_to_entities(db)
        assert n_e == 2          # both survive
        assert redirects == {}   # no collapse
        entities = _canonical_entities(db)
        assert 42 in entities
        assert 99 in entities
        verify_crdt_consistency(db)


# ---------------------------------------------------------------------------
# Migration preserves canonical: Case 2 — outsider doesn't steal slot
# ---------------------------------------------------------------------------


class TestMigrationPreservesCanonical:
    def test_reimport_does_not_lose_to_outsider(self, db):
        """Same entity (IDs 5, 3), outsider (ID 7) with different desc."""
        _seed_entity(
            db,
            [
                (5, "agent_a", "add", '{"agent_a":1}', "python", "concept",
                 "programming language", "", 100.0),
                (3, "agent_b", "add", '{"agent_b":1}', "python", "concept",
                 "programming language", "", 200.0),  # re-import, same fingerprint
                (7, "agent_c", "add", '{"agent_c":1}', "python", "concept",
                 "the snake", "", 300.0),  # outsider, different fingerprint
            ],
        )
        n_e, n_ed, redirects = project_crdt_to_entities(db)
        assert n_e == 2          # python(lang) winner + python(snake)
        assert redirects == {3: 5}  # only same-fingerprint collapse
        entities = _canonical_entities(db)
        assert 5 in entities     # programming language winner
        assert 7 in entities     # the snake survives independently
        assert entities[5]["name"] == "python"
        assert entities[7]["name"] == "python"
        verify_crdt_consistency(db)


# ---------------------------------------------------------------------------
# Fingerprint stability: metadata update doesn't change fingerprint
# ---------------------------------------------------------------------------


class TestFingerprintStabilityOnMetadataUpdate:
    def test_description_update_does_not_change_fingerprint(self, db):
        """Fingerprint computed at inception; metadata updates don't recompute."""
        # Entity created with bare description
        _seed_entity(
            db,
            [
                (42, "agent_a", "add", '{"agent_a":1}', "alice", "person",
                 "lawyer", "fp_alice_42", 100.0),
            ],
        )
        # Same entity enriched later (different VV, same entity_id, same fingerprint)
        _seed_entity(
            db,
            [
                (42, "agent_a", "add", '{"agent_a":3}', "alice", "person",
                 "corporate lawyer at Skadden LLP", "fp_alice_42", 300.0),
            ],
        )
        n_e, n_ed, redirects = project_crdt_to_entities(db)
        assert n_e == 1
        entities = _canonical_entities(db)
        # Fingerprint unchanged (stored at inception)
        row = db.execute(
            "SELECT fingerprint FROM kg_entities WHERE entity_id = 42"
        ).fetchone()
        assert row[0] == "fp_alice_42"


# ---------------------------------------------------------------------------
# Legacy backfill: entities without fingerprints get computed
# ---------------------------------------------------------------------------


class TestLegacyBackfill:
    def test_entity_without_fingerprint_gets_backfilled(self, db):
        """Legacy entity (no fingerprint field in CRDT ops) gets computed."""
        _seed_entity(
            db,
            [
                (42, "agent_a", "add", '{"agent_a":1}', "alice", "person",
                 "corporate lawyer", "", 100.0),  # fingerprint="" (legacy)
            ],
        )
        n_e, n_ed, redirects = project_crdt_to_entities(db)
        # Backfill computed fingerprint from (name, type, description)
        row = db.execute(
            "SELECT fingerprint FROM kg_entities WHERE entity_id = 42"
        ).fetchone()
        assert row[0] != ""  # fingerprint was backfilled
        expected = compute_fingerprint("alice", "person", "corporate lawyer")
        assert row[0] == expected


# ---------------------------------------------------------------------------
# Empty description degrades gracefully: old behavior preserved
# ---------------------------------------------------------------------------


class TestEmptyDescriptionDegradesGracefully:
    def test_empty_description_entities_still_collapse(self, db):
        """No description → same (name, type) → same fingerprint → collapse. Old behavior preserved."""
        _seed_entity(
            db,
            [
                (42, "agent_a", "add", '{"agent_a":1}', "alice", "person", "", "", 100.0),
                (99, "agent_b", "add", '{"agent_b":1}', "alice", "person", "", "", 200.0),
            ],
        )
        n_e, n_ed, redirects = project_crdt_to_entities(db)
        assert n_e == 1
        assert redirects == {42: 99}
        verify_crdt_consistency(db)


class TestOrphanGuardDanglingEdges:
    def test_edge_to_never_created_entity_is_dropped(self, db):
        """An edge whose endpoint references an entity with no entity op
        (never created) must be dropped, never projected as an orphan.

        This closes the C7 gap: the artifact has no durable redirect table
        to fall back on, so project_crdt_to_entities filters against the
        surviving canonical entity IDs before writing kg_edges.
        """
        # Only 'alice' (99) exists. Edge 1→99 references a never-created
        # source (1); edge 2→99 references a never-created target (2).
        _seed_entity(
            db,
            [(99, "agent_b", "add", '{"agent_b":2}', "alice", "person", "", "", 200.0)],
        )
        _seed_edge(
            db,
            [
                (1, 1, 99, "collaborates_with", 1.0, None, "agent_a", '{"agent_a":1}', 110.0),
                (2, 99, 2, "collaborates_with", 1.0, None, "agent_b", '{"agent_b":1}', 210.0),
            ],
        )
        n_e, n_ed, redirects = project_crdt_to_entities(db)
        assert n_e == 1
        assert n_ed == 0, f"expected 0 edges (both dangling), got {n_ed}"
        edges = _canonical_edges(db)
        assert edges == [], f"no edges should be written, got {edges}"
        verify_crdt_consistency(db)

    def test_valid_edge_survives_orphan_guard(self, db):
        """A well-formed edge between two surviving entities is preserved."""
        _seed_entity(
            db,
            [
                (15, "agent_a", "add", '{"agent_a":1}', "bob", "person", "", "", 50.0),
                (99, "agent_b", "add", '{"agent_b":2}', "alice", "person", "", "", 200.0),
            ],
        )
        _seed_edge(
            db,
            [(1, 99, 15, "collaborates_with", 1.0, None, "agent_a", '{"agent_a":3}', 110.0)],
        )
        n_e, n_ed, redirects = project_crdt_to_entities(db)
        assert n_e == 2
        assert n_ed == 1
        assert _canonical_edges(db) == [(99, 15, "collaborates_with")]
        verify_crdt_consistency(db)


class TestOrphanGuardProperty:
    """Orphan guard drops ALL edges with non-canonical endpoints."""

    def test_orphan_guard_drops_non_canonical(self, db):
        """4 edges: valid, tombstoned src, never-created src, both bad.
        Full pipeline keeps only the valid edge."""
        _seed_entity(db, [
            (1, "a", "add", '{"a":1}', "bob", "person", "", "", 50.0),
            (2, "a", "add", '{"a":2}', "alice", "person", "", "", 100.0),
            (3, "a", "add", '{"a":3}', "carol", "person", "", "", 150.0),
            (2, "a", "remove", '{"a":4}', "alice", "person", "", "", 200.0),
        ])
        _seed_edge(db, [
            (1, 1, 3, "related_to", 1.0, None, "a", '{"a":5}', 250.0),
            (2, 2, 1, "related_to", 1.0, None, "a", '{"a":6}', 260.0),
            (3, 99, 3, "related_to", 1.0, None, "a", '{"a":7}', 270.0),
            (4, 99, 2, "related_to", 1.0, None, "a", '{"a":8}', 280.0),
        ])
        project_crdt_to_entities(db)

        # Only edge 1 (1→3) should survive
        edges = _canonical_edges(db)
        assert len(edges) == 1, f"Expected 1 edge, got {len(edges)}"
        assert (1, 3, "related_to") in edges

        # Verify no orphans
        verify_crdt_consistency(db)


# ---------------------------------------------------------------------------
# Property-based tests: CRDT merge invariants
# ---------------------------------------------------------------------------
# These verify the formal properties claimed in §5.3 of the paper:
# commutativity, idempotence, and convergence of the merge pipeline.
# No external dependencies (hypothesis etc.) — uses deterministic
# permutation enumeration over small op sets.


from itertools import permutations


class TestMergeCommutativity:
    """merge_entity_ops produces the same result regardless of operation order."""

    def _ops(self):
        return [
            EntityOp(1, "a", "add", {"a": 1}, "alice", "person", "", fingerprint="", timestamp=100.0),
            EntityOp(1, "b", "add", {"b": 1}, "alice", "person", "", fingerprint="", timestamp=200.0),
            EntityOp(2, "a", "add", {"a": 1}, "bob", "person", "", fingerprint="", timestamp=50.0),
        ]

    def test_all_orderings_produce_same_result(self):
        ops = self._ops()
        reference = merge_entity_ops(ops)
        for perm in permutations(ops):
            assert merge_entity_ops(list(perm)) == reference, (
                f"merge result differs for order {[o.entity_id for o in perm]}"
            )

    def test_duplicate_ops_idempotent(self):
        ops = self._ops()
        once = merge_entity_ops(ops)
        twice = merge_entity_ops(ops + ops)
        assert once == twice


class TestEdgeMergeCommutativity:
    """merge_edge_ops produces the same result regardless of operation order."""

    def _ops(self):
        return [
            EdgeOp(1, 10, 20, "r", 1.0, None, "a", {"a": 1}, 100.0),
            EdgeOp(1, 10, 20, "r", 2.0, None, "b", {"b": 1}, 200.0),
            EdgeOp(2, 30, 40, "s", 1.0, None, "a", {"a": 1}, 50.0),
        ]

    def test_all_orderings_produce_same_result(self):
        ops = self._ops()
        reference = merge_edge_ops(ops)
        for perm in permutations(ops):
            assert merge_edge_ops(list(perm)) == reference, (
                f"edge merge differs for order {[o.edge_id for o in perm]}"
            )

    def test_duplicate_ops_idempotent(self):
        ops = self._ops()
        once = merge_edge_ops(ops)
        twice = merge_edge_ops(ops + ops)
        assert once == twice


class TestPropertyNoOrphan:
    """Every edge endpoint after the full pipeline references a canonical entity."""

    def _run_pipeline(self, entity_rows, edge_rows):
        conn = sqlite3.connect(":memory:")
        conn.executescript(_SCHEMA)
        _seed_entity(conn, entity_rows)
        _seed_edge(conn, edge_rows)
        project_crdt_to_entities(conn)
        return conn

    def test_no_orphan_basic(self):
        conn = self._run_pipeline(
            [(1, "a", "add", '{"a":1}', "x", "t", "", "", 100.0)],
            [(1, 1, 1, "r", 1.0, None, "a", '{"a":1}', 110.0)],
        )
        verify_crdt_consistency(conn)
        conn.close()

    def test_no_orphan_after_dedup(self):
        conn = self._run_pipeline(
            [
                (1, "a", "add", '{"a":1}', "x", "t", "", "", 100.0),
                (2, "b", "add", '{"b":1}', "x", "t", "", "", 200.0),
            ],
            [
                (1, 1, 10, "r", 1.0, None, "a", '{"a":1}', 110.0),
                (2, 2, 10, "r", 1.0, None, "b", '{"b":1}', 210.0),
            ],
        )
        verify_crdt_consistency(conn)
        entities = _canonical_entities(conn)
        assert len(entities) == 1  # both "x" collapsed to one
        conn.close()

    def test_no_orphan_three_way_merge(self):
        conn = self._run_pipeline(
            [
                (1, "a", "add", '{"a":1}', "p", "proj", "", "", 100.0),
                (2, "b", "add", '{"b":1}', "p", "proj", "", "", 200.0),
                (3, "c", "add", '{"c":1}', "p", "proj", "", "", 300.0),
                (10, "a", "add", '{"a":2}', "target", "proj", "", "", 50.0),
            ],
            [
                (1, 1, 10, "depends_on", 1.0, None, "a", '{"a":2}', 110.0),
                (2, 2, 10, "depends_on", 1.0, None, "b", '{"b":2}', 210.0),
                (3, 3, 10, "depends_on", 1.0, None, "c", '{"c":2}', 310.0),
            ],
        )
        verify_crdt_consistency(conn)
        edges = _canonical_edges(conn)
        # All edges should point to the winner (max of 1,2,3 = 3)
        for src, tgt, _rel in edges:
            assert src == 3
        conn.close()


class TestPropertyFingerprintDedup:
    """Entities with same fingerprint collapse; different fingerprints coexist."""

    def test_same_fp_collapses(self):
        state = {
            1: {"name": "alice", "entity_type": "person", "fingerprint": "FP1"},
            2: {"name": "alice", "entity_type": "person", "fingerprint": "FP1"},
        }
        result = entity_dedup_via_crdt(state)
        assert len(result["merged_state"]) == 1
        assert result["redirects"] == {1: 2}

    def test_different_fp_coexist(self):
        state = {
            1: {"name": "alice", "entity_type": "person", "fingerprint": "FP1"},
            2: {"name": "alice", "entity_type": "person", "fingerprint": "FP2"},
        }
        result = entity_dedup_via_crdt(state)
        assert len(result["merged_state"]) == 2
        assert result["redirects"] == {}

    def test_backfill_computes_missing_fp(self):
        state = {
            42: {"name": "alice", "entity_type": "person", "fingerprint": ""},
        }
        result = entity_dedup_via_crdt(state)
        fp = result["merged_state"][42]["fingerprint"]
        assert fp == compute_fingerprint("alice", "person", "")


class TestPropertyRedirectIdempotence:
    """Applying redirect twice yields same result as applying once."""

    def test_idempotent(self):
        redirects = {42: 99, 10: 99}
        edges = {
            1: {"source_id": 42, "target_id": 15, "relation": "r"},
            2: {"source_id": 10, "target_id": 20, "relation": "s"},
            3: {"source_id": 99, "target_id": 30, "relation": "t"},
        }
        once = redirect_edge_ids(edges, redirects)
        twice = redirect_edge_ids(once, redirects)
        assert once == twice


class TestPropertyDeterminism:
    """Pipeline produces identical output for identical input, every time."""

    def test_full_pipeline_deterministic(self):
        """Run the full pipeline 3 times on the same DB; all produce identical state."""
        results = []
        for _ in range(3):
            conn = sqlite3.connect(":memory:")
            conn.executescript(_SCHEMA)
            _seed_entity(conn, [
                (15, "a", "add", '{"a":1}', "bob", "person", "", "", 50.0),
                (42, "a", "add", '{"a":2}', "alice", "person", "", "", 100.0),
                (99, "b", "add", '{"b":1}', "alice", "person", "", "", 200.0),
            ])
            _seed_edge(conn, [
                (1, 42, 15, "collaborates_with", 1.0, None, "a", '{"a":3}', 110.0),
                (2, 99, 23, "collaborates_with", 1.0, None, "b", '{"b":3}', 210.0),
            ])
            project_crdt_to_entities(conn)
            entities = _canonical_entities(conn)
            edges = _canonical_edges(conn)
            conn.close()
            results.append((entities, edges))

        assert results[0] == results[1] == results[2]


class TestPropertyConvergence:
    """True CRDT convergence: different operation orderings produce identical canonical state."""

    def test_all_permutations_converge(self):
        """All 24 permutations of 4 entity ops produce identical canonical state."""
        from itertools import permutations

        ops = [
            (10, "a", "add", '{"a":1}', "project:x", "project", "", "", 100.0),
            (20, "b", "add", '{"b":1}', "project:x", "project", "", "", 200.0),
            (30, "c", "add", '{"c":1}', "project:x", "project", "", "", 300.0),
            (15, "a", "add", '{"a":2}', "target", "proj", "", "", 50.0),
        ]
        edge_ops = [
            (1, 10, 15, "depends_on", 1.0, None, "a", '{"a":2}', 110.0),
            (2, 20, 15, "depends_on", 1.0, None, "b", '{"b":2}', 210.0),
            (3, 30, 15, "depends_on", 1.0, None, "c", '{"c":2}', 310.0),
        ]

        results = set()
        for perm in permutations(ops):
            conn = sqlite3.connect(":memory:")
            conn.executescript(_SCHEMA)
            _seed_entity(conn, list(perm))
            _seed_edge(conn, edge_ops)
            project_crdt_to_entities(conn)
            entities = _canonical_entities(conn)
            edges = _canonical_edges(conn)
            conn.close()
            results.add((tuple(sorted(entities.keys())), tuple(sorted(edges))))

        assert len(results) == 1, f"Convergence violated: {len(results)} distinct outputs from 24 permutations"

    def test_cross_peer_convergence(self):
        """Two peers with partially-overlapping ops reach same state."""
        from crdt_projection import merge_entity_ops, entity_dedup_via_crdt, EntityOp
        ops_a = [
            EntityOp(10, "a", "add", {"a": 1}, "project:x", "project", "", "", 100.0),
            EntityOp(20, "b", "add", {"b": 1}, "project:x", "project", "", "", 200.0),
        ]
        ops_b = [
            EntityOp(20, "b", "add", {"b": 1}, "project:x", "project", "", "", 200.0),
            EntityOp(30, "c", "add", {"c": 1}, "project:x", "project", "", "", 300.0),
        ]

        # Merge both sets
        all_ops = ops_a + ops_b
        merged = merge_entity_ops(all_ops)
        dedup = entity_dedup_via_crdt(merged)
        canonical = dedup["merged_state"]
        redirects = dedup["redirects"]

        # Verify: exactly one canonical entity (max of 10, 20, 30 = 30)
        assert len(canonical) == 1
        assert 30 in canonical
        assert redirects == {10: 30, 20: 30}
