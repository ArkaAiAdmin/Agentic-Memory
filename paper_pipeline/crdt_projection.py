"""
Standalone three-phase CRDT projection pipeline.

Extracted from agentic-memory's save_pipeline.py / CRDT merge logic.
No agent-specific imports. Uses only stdlib (sqlite3, dataclasses, dict, typing).

Phase 1:  merge_entity_ops  — 2P-Set membership + LWW per field
Phase 2:  entity_dedup_via_crdt  — group by (name, type), pick winner, build redirect map
Phase 3:  redirect_edge_ids  — rewrite edge endpoints through redirect map

End-to-end:  project_crdt_to_entities  (phases 1–3 + DB I/O)
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection



# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class EntityOp:
    entity_id: int
    agent_id: str
    op: str  # "add" | "remove"
    version_vector: Dict[str, int] = field(default_factory=dict)
    name: str = ""
    entity_type: str = ""
    description: str = ""
    fingerprint: str = ""  # computed at inception, immutable
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
# Fingerprint computation
# ---------------------------------------------------------------------------


def compute_fingerprint(name: str, entity_type: str, description: str = "") -> str:
    """Compute inception fingerprint for entity identity.

    Same fingerprint → same entity (dedup with max(entity_id) tiebreaker).
    Different fingerprint → different entity (coexist, even if name+type match).

    Canonicalization pipeline:
    1. NFKC normalization (handles NBSP → space, fi-ligature → "fi", etc.)
    2. Strip format characters (category Cf: zero-width spaces, RTL marks, etc.)
    3. Lowercase + collapse whitespace
    """
    def canonical(s: str) -> str:
        # Step 1: NFKC compatibility decomposition + recomposition
        s = unicodedata.normalize("NFKC", s)
        # Step 2: Strip format/control characters (Cf, Cc, Co categories)
        # This removes ZWSP (\u200B), LTR/RTL marks, soft hyphens, etc.
        s = "".join(c for c in s if unicodedata.category(c) not in ("Cf", "Cc", "Co"))
        # Step 3: Lowercase, strip, collapse whitespace
        return " ".join(s.lower().strip().split())

    payload = f"{canonical(name)}|{canonical(entity_type)}|{canonical(description)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Version-vector helpers
# ---------------------------------------------------------------------------


def vv_dominates(a: Dict[str, int], b: Dict[str, int]) -> bool:
    """Return True iff version vector a causally dominates b.

    a dominates b when:
      - for every peer p: a[p] >= b[p]
      - for at least one peer p: a[p] >  b[p]
    """
    if not a or not b:
        return False
    all_peers = set(a) | set(b)
    ge = all(a.get(p, 0) >= b.get(p, 0) for p in all_peers)
    gt = any(a.get(p, 0) > b.get(p, 0) for p in all_peers)
    return ge and gt


def vv_merge(a: Dict[str, int], b: Dict[str, int]) -> Dict[str, int]:
    """Component-wise maximum of two version vectors."""
    result = dict(a)
    for peer, count in b.items():
        result[peer] = max(result.get(peer, 0), count)
    return result


def _serialise_vv(v: Dict[str, int]) -> str:
    """Deterministic string representation of a version vector for tiebreaking."""
    return json.dumps(sorted((v or {}).items()))


# ---------------------------------------------------------------------------
# Phase 1: Entity CRDT merge
# ---------------------------------------------------------------------------


def merge_entity_ops(ops: Iterable[EntityOp]) -> Dict[int, Dict[str, Any]]:
    """Merge entity ops using 2P-Set membership + LWW per field.

    Args:
        ops: All entity operations from the CRDT log.

    Returns:
        {entity_id: {tombstone, name, entity_type, description, fingerprint}}
    """
    by_entity: Dict[int, List[EntityOp]] = {}
    for op in ops:
        by_entity.setdefault(op.entity_id, []).append(op)

    result: Dict[int, Dict[str, Any]] = {}

    for entity_id, ops_for_entity in by_entity.items():
        # Stable sort: timestamp first, then serialized VV for deterministic tiebreak.
        sorted_ops = sorted(
            ops_for_entity,
            key=lambda o: (
                o.timestamp,
                _serialise_vv(o.version_vector),
            ),
        )

        adds = [o for o in sorted_ops if o.op == "add"]
        removes = [o for o in sorted_ops if o.op == "remove"]

        if not adds:
            continue

        # 2P-Set: tombstoned if any remove causally follows any add.
        is_tombstoned = any(
            vv_dominates(rem_op.version_vector, add_op.version_vector)
            for add_op in adds
            for rem_op in removes
        )
        if is_tombstoned:
            continue

        # LWW per metadata field: uses causal partial order (vv_dominates).
        # If one op's version vector dominates another's, it wins.
        # Truly concurrent ops (neither dominates) fall back to
        # (timestamp desc, agent_id asc) — a proper total order.
        def _field_winner(field: str) -> str:
            candidates = [o for o in adds if getattr(o, field, "")]
            if not candidates:
                return ""
            winner = candidates[0]
            for candidate in candidates[1:]:
                if vv_dominates(
                    candidate.version_vector, winner.version_vector
                ):
                    winner = candidate
                elif not vv_dominates(
                    winner.version_vector, candidate.version_vector
                ):
                    if (
                        candidate.timestamp > winner.timestamp
                        or (
                            candidate.timestamp == winner.timestamp
                            and candidate.agent_id < winner.agent_id
                        )
                    ):
                        winner = candidate
            return str(getattr(winner, field))

        # Fingerprint: immutable at inception; take from first add op that has one.
        # All adds for the same entity_id carry the same fingerprint.
        fp = ""
        for a in adds:
            if a.fingerprint:
                fp = a.fingerprint
                break

        result[entity_id] = {
            "tombstone": False,
            "name": _field_winner("name"),
            "entity_type": _field_winner("entity_type"),
            "description": _field_winner("description"),
            "fingerprint": fp,
        }

    return result


# ---------------------------------------------------------------------------
# Phase 2: Entity dedup + redirect map
# ---------------------------------------------------------------------------


def entity_dedup_via_crdt(
    merged_state: Dict[int, Dict[str, Any]],
) -> Dict[str, Any]:
    """Resolve name-collisions; produce redirect map.

    Groups surviving entities by their inception fingerprint (see §2.5).
    Each fingerprint identifies one logical entity; entities with the same
    fingerprint are concurrent representations of the same real-world concept
    and collapse via max(entity_id). Entities with different fingerprints are
    distinct, even if they share the same (name, entity_type) pair.

    Args:
        merged_state: output of merge_entity_ops.

    Returns:
        {"merged_state": {winner_id: state}, "redirects": {loser_id: winner_id}}
    """
    by_fingerprint: Dict[str, List[int]] = {}
    for entity_id, info in merged_state.items():
        if info.get("tombstone"):
            continue
        fp = info.get("fingerprint", "")
        if not fp:
            # Legacy: compute from metadata (backfill at projection time)
            fp = compute_fingerprint(
                info.get("name", ""),
                info.get("entity_type", ""),
                info.get("description", ""),
            )
            info["fingerprint"] = fp
        by_fingerprint.setdefault(fp, []).append(entity_id)

    deduped: Dict[int, Dict[str, Any]] = {}
    redirects: Dict[int, int] = {}

    for _fp, ids in by_fingerprint.items():
        if len(ids) == 1:
            deduped[ids[0]] = merged_state[ids[0]]
            continue

        winner_id = max(ids)  # deterministic LWW tiebreaker
        deduped[winner_id] = merged_state[winner_id]
        for loser_id in ids:
            if loser_id != winner_id:
                redirects[loser_id] = winner_id

    return {"merged_state": deduped, "redirects": redirects}


# ---------------------------------------------------------------------------
# Phase 3: Edge redirect + projection
# ---------------------------------------------------------------------------


def redirect_edge_ids(
    edge_state: Dict[int, Dict[str, Any]],
    redirects: Dict[int, int],
) -> Dict[int, Dict[str, Any]]:
    """Rewrite edge endpoints through the redirect map.

    Idempotent: applying it twice is equivalent to applying once,
    because winner IDs never appear as keys in the redirect map.

    Args:
        edge_state:  {edge_id: {source_id, target_id, relation, weight, ...}}
        redirects:   {loser_id: winner_id}

    Returns:
        Rewritten edge_state.
    """
    if not redirects:
        return edge_state

    remapped: Dict[int, Dict[str, Any]] = {}
    for edge_id, info in edge_state.items():
        new_info = dict(info)
        if new_info["source_id"] in redirects:
            new_info["source_id"] = redirects[new_info["source_id"]]
        if new_info["target_id"] in redirects:
            new_info["target_id"] = redirects[new_info["target_id"]]
        remapped[edge_id] = new_info
    return remapped


def merge_edge_ops(ops: Iterable[EdgeOp]) -> Dict[int, Dict[str, Any]]:
    """Merge edge ops using causal dominance (vv_dominates) with timestamp/agent tiebreak.

    Pre-sorts edge operations canonically by (timestamp, _serialise_vv, agent_id)
    to eliminate arrival-order non-transitivity during the fold.
    """
    by_edge: Dict[int, List[EdgeOp]] = {}
    for op in ops:
        by_edge.setdefault(op.edge_id, []).append(op)

    result: Dict[int, Dict[str, Any]] = {}
    for edge_id, ops_for_edge in by_edge.items():
        sorted_ops = sorted(
            ops_for_edge,
            key=lambda o: (
                o.timestamp,
                _serialise_vv(o.version_vector),
                o.agent_id,
            ),
        )
        winner = sorted_ops[0]
        for candidate in sorted_ops[1:]:
            if vv_dominates(candidate.version_vector, winner.version_vector):
                winner = candidate
            elif not vv_dominates(winner.version_vector, candidate.version_vector):
                if (
                    candidate.timestamp > winner.timestamp
                    or (
                        candidate.timestamp == winner.timestamp
                        and candidate.agent_id < winner.agent_id
                    )
                ):
                    winner = candidate
        result[edge_id] = {
            "source_id": winner.source_id,
            "target_id": winner.target_id,
            "relation": winner.relation,
            "weight": winner.weight,
            "valid_at": winner.valid_at,
        }
    return result


# ---------------------------------------------------------------------------
# DB I/O helpers
# ---------------------------------------------------------------------------

_ENTITY_COLS = [
    "entity_id",
    "agent_id",
    "op",
    "version_vector",
    "name",
    "entity_type",
    "description",
    "fingerprint",
    "timestamp",
]

_EDGE_COLS = [
    "edge_id",
    "source_id",
    "target_id",
    "relation",
    "weight",
    "valid_at",
    "agent_id",
    "version_vector",
    "timestamp",
]


def _dataclass_from_rows(cls, rows: Iterable[tuple], cols: list) -> list:
    """Convert DB rows (tuples) to dataclass instances."""
    return [cls(**dict(zip(cols, row))) for row in rows]


def _load_entity_state(conn: AnyConnection) -> List[EntityOp]:
    """Load all rows from kg_entity_crdt into EntityOp instances."""
    rows = conn.execute(
        "SELECT {} FROM kg_entity_crdt".format(", ".join(_ENTITY_COLS))
    ).fetchall()

    ops: List[EntityOp] = []
    for row in rows:
        d = dict(zip(_ENTITY_COLS, row))
        d["version_vector"] = _parse_vv(d.pop("version_vector", "{}"))
        ops.append(EntityOp(**d))
    return ops


def _load_edge_state(conn: AnyConnection) -> List[EdgeOp]:
    """Load all rows from kg_edge_crdt into EdgeOp instances."""
    rows = conn.execute(
        "SELECT {} FROM kg_edge_crdt".format(", ".join(_EDGE_COLS))
    ).fetchall()

    ops: List[EdgeOp] = []
    for row in rows:
        d = dict(zip(_EDGE_COLS, row))
        d["version_vector"] = _parse_vv(d.pop("version_vector", "{}"))
        ops.append(EdgeOp(**d))
    return ops


def _apply_entities(
    conn: AnyConnection, canonical: Dict[int, Dict[str, Any]]
) -> int:
    """Write canonical entities to kg_entities; replace all existing rows.

    Returns number of rows written.
    """
    conn.execute("DELETE FROM kg_entities")
    count = 0
    for entity_id, info in canonical.items():
        fp = info.get("fingerprint", "")
        if not fp:
            fp = compute_fingerprint(
                info["name"], info.get("entity_type", ""),
                info.get("description", ""),
            )
        conn.execute(
            """INSERT INTO kg_entities (entity_id, name, entity_type, mentions, fingerprint)
               VALUES (?, ?, ?, ?, ?)""",
            (
                entity_id,
                info["name"],
                info["entity_type"],
                1,
                fp,
            ),
        )
        count += 1
    return count


def _apply_edges(
    conn: AnyConnection, edge_state: Dict[int, Dict[str, Any]]
) -> int:
    """Write projected edges to kg_edges; replace all existing rows.

    Returns number of rows written.
    """
    conn.execute("DELETE FROM kg_edges")
    count = 0
    for _edge_id, info in edge_state.items():
        conn.execute(
            """INSERT INTO kg_edges (source_id, target_id, relation, weight, valid_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                info["source_id"],
                info["target_id"],
                info["relation"],
                info.get("weight", 1.0),
                info.get("valid_at"),
            ),
        )
        count += 1
    return count


# ---------------------------------------------------------------------------
# End-to-end projection
# ---------------------------------------------------------------------------


def project_crdt_to_entities(
    conn: AnyConnection,
) -> Tuple[int, int, Dict[int, int]]:
    """Run Phases 1–3 and write to canonical tables.

    Args:
        conn: open sqlite3 connection to the database.

    Returns:
        (entities_written, edges_written, redirects_map)
    """
    # Phase 1
    entity_ops = _load_entity_state(conn)
    merged = merge_entity_ops(entity_ops)

    # Phase 2
    dedup = entity_dedup_via_crdt(merged)
    canonical = dedup["merged_state"]
    redirects = dedup["redirects"]

    n_entities = _apply_entities(conn, canonical)

    # Phase 3
    edge_ops = _load_edge_state(conn)
    merged_edges = merge_edge_ops(edge_ops)
    if redirects:
        merged_edges = redirect_edge_ids(merged_edges, redirects)
    # Orphan guard (C7): reconcile edges to surviving canonical entities.
    # Phase 3 has already rewritten merged-away endpoints through `redirects`
    # (loser -> winner). Any endpoint that still fails to resolve to a canonical
    # entity after redirection references a tombstoned or never-created entity,
    # which has no winner: drop that edge rather than persisting an orphan.
    # The production path uses resolve_edge_endpoints against the durable
    # kg_entity_redirect table (rewriting merged-away endpoints to their winner)
    # and rejects edges to tombstoned entities via verify_crdt_consistency; the
    # standalone artifact has no such table, so we filter against the surviving
    # canonical entity IDs after applying the in-memory redirect map.
    canonical_ids = set(canonical.keys())
    reconciled = {}
    for eid, info in merged_edges.items():
        src = redirects.get(info["source_id"], info["source_id"])
        tgt = redirects.get(info["target_id"], info["target_id"])
        if src in canonical_ids and tgt in canonical_ids:
            new_info = dict(info)
            new_info["source_id"] = src
            new_info["target_id"] = tgt
            reconciled[eid] = new_info
    merged_edges = reconciled
    n_edges = _apply_edges(conn, merged_edges)

    conn.commit()
    return n_entities, n_edges, redirects


# ---------------------------------------------------------------------------
# Verification helpers (VV parsing / serialisation)
# ---------------------------------------------------------------------------


def _serialise_vv(v: Dict[str, int]) -> str:
    """Deterministic serialisation for stable sorting (JSON, matching production)."""
    return json.dumps(v, sort_keys=True, separators=(",", ":"))


def _parse_vv(s: str) -> Dict[str, int]:
    """Deserialise a version vector from JSON format (matching production)."""
    if not s or s == "{}":
        return {}
    return json.loads(s)


def verify_crdt_consistency(conn: AnyConnection) -> bool:
    """Assert no-orphan invariant: every edge endpoint must exist in kg_entities.

    Returns True if invariant holds, raises AssertionError otherwise.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM kg_edges e "
        "WHERE e.source_id NOT IN (SELECT entity_id FROM kg_entities) "
        "   OR e.target_id NOT IN (SELECT entity_id FROM kg_entities)"
    ).fetchone()
    orphan_count = row[0] if row else 0
    assert orphan_count == 0, (
        f"no-orphan invariant violated: {orphan_count} edge(s) reference "
        f"non-canonical entities"
    )
    return True


def _seed_scenario_1(conn: AnyConnection) -> None:
    """Seed DB with the alice/bob/charlie scenario (Section 3.2)."""
    conn.executemany(
        "INSERT INTO kg_entity_crdt "
        "(entity_id, agent_id, op, version_vector, name, entity_type, "
        "description, fingerprint, timestamp) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (15, "agent_a", "add", '{"agent_a":1}', "bob", "person", "", "", 50.0),
            (23, "agent_b", "add", '{"agent_b":1}', "charlie", "person", "", "", 150.0),
            (42, "agent_a", "add", '{"agent_a":2}', "alice", "person", "", "", 100.0),
            (99, "agent_b", "add", '{"agent_b":2}', "alice", "person", "", "", 200.0),
        ],
    )
    # Edge ops reference the original IDs (42, 99) — redirect fixes them.
    conn.executemany(
        "INSERT INTO kg_edge_crdt "
        "(edge_id, source_id, target_id, relation, weight, valid_at, "
        "agent_id, version_vector, timestamp) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (1, 42, 15, "collaborates_with", 1.0, None, "agent_a", '{"agent_a":3}', 110.0),
            (2, 99, 23, "collaborates_with", 1.0, None, "agent_b", '{"agent_b":3}', 210.0),
        ],
    )


if __name__ == "__main__":
    import tempfile, pathlib

    schema = """
    CREATE TABLE kg_entity_crdt (
        op_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id      INTEGER NOT NULL, agent_id TEXT NOT NULL,
        op             TEXT    NOT NULL CHECK (op IN ('add','remove')),
        version_vector TEXT    NOT NULL, name TEXT, entity_type TEXT,
        description    TEXT, fingerprint TEXT, timestamp REAL NOT NULL,
        applied        INTEGER DEFAULT 0, tenant_id TEXT DEFAULT 'default'
    );
    CREATE TABLE kg_edge_crdt (
        op_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        edge_id        INTEGER NOT NULL, source_id INTEGER NOT NULL,
        target_id      INTEGER NOT NULL, relation TEXT NOT NULL,
        weight         REAL NOT NULL DEFAULT 1.0, valid_at TEXT,
        agent_id       TEXT NOT NULL, version_vector TEXT NOT NULL,
        timestamp      REAL NOT NULL,
        applied        INTEGER DEFAULT 0, tenant_id TEXT DEFAULT 'default'
    );
    CREATE TABLE kg_entities (
        entity_id INTEGER PRIMARY KEY, name TEXT NOT NULL, entity_type TEXT NOT NULL,
        mentions INTEGER DEFAULT 1, fingerprint TEXT,
        UNIQUE(fingerprint)
    );
    CREATE TABLE kg_edges (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL, relation TEXT NOT NULL, weight REAL DEFAULT 1.0,
        valid_at TEXT
    );
    CREATE TABLE kg_entity_redirect (
        loser_id    INTEGER NOT NULL, winner_id   INTEGER NOT NULL,
        reason      TEXT    DEFAULT 'collision',
        created_at  TEXT    DEFAULT (datetime('now')),
        PRIMARY KEY (loser_id, winner_id)
    );
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    conn = sqlite3.connect(db_path)
    conn.executescript(schema)
    _seed_scenario_1(conn)
    n_e, n_ed, redirects = project_crdt_to_entities(conn)
    verify_crdt_consistency(conn)
    conn.close()
    pathlib.Path(db_path).unlink()

    print(f"entities={n_e}, edges={n_ed}, redirects={redirects}")
    assert n_e == 3, f"expected 3 entities, got {n_e}"
    assert n_ed == 2, f"expected 2 edges, got {n_ed}"
    assert redirects == {42: 99}, f"unexpected redirects: {redirects}"
    print("Smoke test passed.")
