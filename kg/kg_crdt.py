"""S2 (Graph CRDTs for Peer-to-Peer KG Replication).

This module implements Causal Graph CRDTs for the Knowledge Graph,
enabling offline-first multi-agent setups to sync KG state without
data loss.

Background
----------
The pre-S2 system used a Last-Writer-Wins (LWW) approach for KG
entity and edge updates, which is NOT a proper CRDT. In a multi-peer
setup (e.g., laptop + desktop), this caused:

  1. Silent data loss when two peers updated the same entity offline
  2. Duplicate entities when names collided across peers
  3. Inconsistent edge sets because there was no merge protocol

The S2 design uses:

  - **Entity CRDT**: 2P-Set (two-phase set) for membership +
    LWW-Register per field for metadata. Each peer has a unique
    agent_id; deletes are only "won" by the peer that added the
    entity (add wins on concurrent add/remove).

  - **Edge CRDT**: Add-only set with LWW on (weight, valid_at)
    metadata. Edges are never deleted — they're marked invalid via
    `invalid_at` (already supported in the schema).

Both CRDTs satisfy the four properties:
  1. Commutativity:    merge(a, b) == merge(b, a)
  2. Associativity:    merge(merge(a, b), c) == merge(a, merge(b, c))
  3. Idempotence:      merge(a, a) == a
  4. Convergence:      all replicas applying the same set of
                       operations in any order reach the same state.

Storage
-------
Two new tables (added in migration 021):

  kg_entity_crdt (
    entity_id     INTEGER PRIMARY KEY,
    agent_id      TEXT NOT NULL,    -- which peer added/removed
    version_vector TEXT NOT NULL,   -- JSON: {"agent1": clock1, ...}
    op            TEXT NOT NULL,    -- "add" or "remove"
    timestamp     REAL NOT NULL
  )

  kg_edge_crdt (
    edge_id       INTEGER PRIMARY KEY,
    source_id     INTEGER NOT NULL,
    target_id     INTEGER NOT NULL,
    relation      TEXT NOT NULL,
    weight        REAL NOT NULL,
    version_vector TEXT NOT NULL,
    agent_id      TEXT NOT NULL,
    timestamp     REAL NOT NULL
  )

The original ``kg_entities`` and ``kg_edges`` tables hold the
"current" state. The CRDT tables hold the per-peer operations
that, when merged, reproduce the state.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection


logger = logging.getLogger(__name__)

__all__ = [
    "EntityOp",
    "EdgeOp",
    "merge_entity_ops",
    "merge_edge_ops",
    "apply_entity_crdt_to_db",
    "apply_edge_crdt_to_db",
    "compute_entity_crdt_state",
    "compute_edge_crdt_state",
    "ensure_kg_crdt_schema",
]


# ---------------------------------------------------------------------------
# Version vectors
# ---------------------------------------------------------------------------


def make_vv() -> dict[str, int]:
    return {}


def vv_increment(vv: dict[str, int], agent_id: str) -> dict[str, int]:
    """Return a new version vector with ``agent_id``'s clock bumped.

    Version vectors are immutable in this implementation — every
    operation returns a new dict. This is the standard CRDT
    convention (avoids aliasing bugs).
    """
    new_vv = dict(vv)
    new_vv[agent_id] = new_vv.get(agent_id, 0) + 1
    return new_vv


def vv_dominates(a: dict[str, int], b: dict[str, int]) -> bool:
    """True if ``a`` dominates ``b`` (a is causally after b).

    a dominates b iff for every agent, a's clock >= b's clock,
    AND a has at least one strict greater.
    """
    a_at_least = all(a.get(agent, 0) >= b.get(agent, 0) for agent in set(a) | set(b))
    a_strict = any(a.get(agent, 0) > b.get(agent, 0) for agent in set(a) | set(b))
    return a_at_least and a_strict


def vv_concurrent(a: dict[str, int], b: dict[str, int]) -> bool:
    """True if ``a`` and ``b`` are concurrent (neither dominates)."""
    return not vv_dominates(a, b) and not vv_dominates(b, a) and a != b


def vv_merge(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    """Component-wise maximum of two version vectors."""
    result = dict(a)
    for agent, clock in b.items():
        if clock > result.get(agent, 0):
            result[agent] = clock
    return result


def vv_sum(v: dict[str, int]) -> int:
    """Total counter value across all peers — used as primary LWW sort key."""
    return sum(v.values())


def _serialise_vv(v: dict[str, int]) -> str:
    """Deterministic serialisation for stable sorting (paper canonical form)."""
    return ",".join(f"{k}:{v[k]}" for k in sorted(v))


def _parse_vv(s: str) -> dict[str, int]:
    """Deserialise a version vector from 'peer:count,...' format."""
    result: dict[str, int] = {}
    if not s or s == "{}":
        return result
    for item in s.split(","):
        item = item.strip()
        if ":" in item:
            peer, _, count = item.partition(":")
            result[peer] = int(count)
    return result


# ---------------------------------------------------------------------------
# Entity CRDT: 2P-Set + LWW per field
# ---------------------------------------------------------------------------


@dataclass
class EntityOp:
    """A single entity CRDT operation.

    Attributes:
        entity_id: Stable unique ID for the entity (UUID or hash of
            canonical name). Peers must agree on the ID for ops to
            merge correctly.
        agent_id: The peer that issued this op.
        op: "add" or "remove".
        version_vector: Causal context for this op.
        name: Canonical entity name (used for display and LWW).
        entity_type: Optional category (concept, person, etc.).
        description: Human-readable description.
        timestamp: When this op was created (seconds since epoch).
    """

    entity_id: int
    agent_id: str
    op: str  # "add" | "remove"
    version_vector: dict[str, int]
    name: str
    entity_type: str
    description: str
    timestamp: float


def merge_entity_ops(ops: Iterable[EntityOp]) -> dict[int, dict[str, Any]]:
    """Merge a set of EntityOps into a per-entity state.

    Returns a dict ``{entity_id: {"tombstone": bool, "name": str,
    "entity_type": str, "description": str}}``. The 2P-Set semantics
    are: an entity exists iff there's an "add" op that is not
    preceded by a "remove" op from the same peer that added it
    (add wins on concurrent add/remove). Metadata fields use LWW:
    the field value with the highest version_vector clock wins.

    This function is pure (no DB I/O). The result can be passed to
    ``apply_entity_crdt_to_db`` to persist.
    """
    by_entity: dict[int, list[EntityOp]] = {}
    for op in ops:
        by_entity.setdefault(op.entity_id, []).append(op)

    result: dict[int, dict[str, Any]] = {}
    for entity_id, ops_for_entity in by_entity.items():
        # Sort by timestamp to get a deterministic order. Ties
        # broken by version_vector comparison (higher wins).
        sorted_ops = sorted(
            ops_for_entity,
            key=lambda o: (o.timestamp, _serialise_vv(o.version_vector)),
        )
        # 2P-Set: an add wins if no later remove from the same peer.
        # If add and remove are concurrent, add wins.
        adds = [o for o in sorted_ops if o.op == "add"]
        removes = [o for o in sorted_ops if o.op == "remove"]
        if not adds:
            continue
        # Add is tombstoned only if there is a remove from a peer
        # that causally follows the add. Concurrent add/remove = add
        # wins (standard 2P-Set semantics).
        is_tombstoned = False
        for add_op in adds:
            for remove_op in removes:
                if vv_dominates(remove_op.version_vector, add_op.version_vector):
                    is_tombstoned = True
                    break
            if is_tombstoned:
                break
        if is_tombstoned:
            continue

        # LWW per metadata field: pick the op with the highest
        # version_vector according to causal partial order.
        # Ties broken by (timestamp desc, agent_id asc) — a proper
        # total order, not sum() which is not a partial order.
        def _winner(field_name: str) -> str:
            field_ops = [o for o in adds if getattr(o, field_name, "")]
            if not field_ops:
                return ""
            if len(field_ops) == 1:
                return str(field_ops[0].__dict__[field_name])
            winner_op = field_ops[0]
            for candidate in field_ops[1:]:
                if vv_dominates(candidate.version_vector, winner_op.version_vector):
                    winner_op = candidate
                elif not vv_dominates(winner_op.version_vector, candidate.version_vector):
                    if (
                        candidate.timestamp > winner_op.timestamp
                        or (
                            candidate.timestamp == winner_op.timestamp
                            and candidate.agent_id < winner_op.agent_id
                        )
                    ):
                        winner_op = candidate
            return str(winner_op.__dict__[field_name])

        result[entity_id] = {
            "tombstone": False,
            "name": _winner("name"),
            "entity_type": _winner("entity_type"),
            "description": _winner("description"),
        }
    return result


def compute_entity_crdt_state(conn: AnyConnection) -> dict[int, dict[str, Any]]:
    """Read all entity CRDT ops from the DB and merge them.

    This is the canonical state of the entity set after merging all
    known ops. Used by the sync protocol to send the current state
    to a peer.
    """
    rows = conn.execute(
        """
        SELECT entity_id, agent_id, op, version_vector, name,
               entity_type, description, timestamp
        FROM kg_entity_crdt
        """
    ).fetchall()
    ops = [
        EntityOp(
            entity_id=row[0],
            agent_id=row[1],
            op=row[2],
            version_vector=json.loads(row[3]) if row[3] else {},
            name=row[4] or "",
            entity_type=row[5] or "",
            description=row[6] or "",
            timestamp=row[7] or 0.0,
        )
        for row in rows
    ]
    return merge_entity_ops(ops)


def apply_entity_crdt_to_db(
    conn: AnyConnection,
    state: dict[int, dict[str, Any]],
) -> int:
    """Apply a merged entity state to the kg_entities table.

    Sprint 2.3: Preserves entity_id from CRDT state to maintain
    stable identity across peers. Falls back to auto-increment
    when entity_id is not in state.

    Returns the number of entities written. This is idempotent: the
    caller can re-apply the same state without creating duplicates
    because we use ``INSERT OR REPLACE`` on the (name, entity_type)
    unique constraint.
    """
    written = 0
    for entity_id, info in state.items():
        if info.get("tombstone"):
            continue
        if info.get("_redirect"):
            # This entity was merged - skip
            continue
        # Sprint 2.3: Include entity_id and fingerprint in INSERT
        conn.execute(
            """
            INSERT OR REPLACE INTO kg_entities
                (id, name, entity_type, fingerprint, mentions, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, datetime('now'), datetime('now'))
            """,
            (
                entity_id,
                info["name"],
                info.get("entity_type", ""),
                info.get("fingerprint"),
            ),
        )
        written += 1
    return written


# ---------------------------------------------------------------------------
# Edge CRDT: Add-only set with LWW on metadata
# ---------------------------------------------------------------------------


@dataclass
class EdgeOp:
    """A single edge CRDT operation.

    Edges are add-only (no remove op) — deletion is represented by
    setting ``invalid_at`` to a timestamp. This is the standard
    "tombstone" pattern for graph CRDTs and avoids the
    add/remove-race in the 2P-Set.
    """

    edge_id: int  # unique per (source, target, relation) tuple
    source_id: int
    target_id: int
    relation: str
    weight: float
    valid_at: Optional[str]
    agent_id: str
    version_vector: dict[str, int]
    timestamp: float


def _edge_key(source_id: int, target_id: int, relation: str) -> int:
    """Stable hash for the (source, target, relation) triple.

    Two peers that see the same edge must agree on the edge_id so
    their ops merge correctly. We use a deterministic 64-bit hash
    of the three fields, which is collision-resistant enough for
    practical use.
    """
    import hashlib

    raw = f"{source_id}|{target_id}|{relation}".encode("utf-8")
    h = hashlib.sha256(raw).digest()
    # Take 8 bytes, convert to signed int for SQLite INTEGER PRIMARY KEY.
    return int.from_bytes(h[:8], "big", signed=True) % (2**63)


def merge_edge_ops(ops: Iterable[EdgeOp]) -> dict[int, dict[str, Any]]:
    """Merge edge ops using causal dominance (vv_dominates) with timestamp/agent tiebreak.

    The correct CRDT merge: if one op's version vector causally dominates
    another's, it wins. Only truly concurrent ops (neither dominates) fall
    back to (timestamp desc, agent_id asc) tiebreak. This preserves causal
    ordering guarantees — a later op from the same or causally-following
    peer always wins over an earlier one.

    The paper previously used vv_sum as the primary sort key, but that
    conflates concurrent ops with different component-wise clocks
    (e.g. {A:3,B:0} vs {A:0,B:3} both sum to 3). vv_dominates is the
    correct partial-order comparator and is what production uses.
    """
    by_edge: dict[int, list[EdgeOp]] = {}
    for op in ops:
        by_edge.setdefault(op.edge_id, []).append(op)

    result: dict[int, dict[str, Any]] = {}
    for edge_id, ops_for_edge in by_edge.items():
        winner = ops_for_edge[0]
        for candidate in ops_for_edge[1:]:
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


def compute_edge_crdt_state(conn: AnyConnection) -> dict[int, dict[str, Any]]:
    """Read all edge CRDT ops from the DB and merge them."""
    rows = conn.execute(
        """
        SELECT edge_id, source_id, target_id, relation, weight,
               valid_at, agent_id, version_vector, timestamp
        FROM kg_edge_crdt
        """
    ).fetchall()
    ops = [
        EdgeOp(
            edge_id=row[0],
            source_id=row[1],
            target_id=row[2],
            relation=row[3] or "related_to",
            weight=row[4] or 1.0,
            valid_at=row[5],
            agent_id=row[6] or "",
            version_vector=json.loads(row[7]) if row[7] else {},
            timestamp=row[8] or 0.0,
        )
        for row in rows
    ]
    return merge_edge_ops(ops)


def apply_edge_crdt_to_db(
    conn: AnyConnection,
    state: dict[int, dict[str, Any]],
) -> int:
    """Apply a merged edge state to the kg_edges table."""
    written = 0
    for _edge_id, info in state.items():
        conn.execute(
            """
            INSERT OR REPLACE INTO kg_edges
                (source_id, target_id, relation, weight, valid_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                info["source_id"],
                info["target_id"],
                info.get("relation", "related_to"),
                info.get("weight", 1.0),
                info.get("valid_at"),
            ),
        )
        written += 1
    return written


# ---------------------------------------------------------------------------
# Schema setup
# ---------------------------------------------------------------------------


_KG_CRDT_SCHEMA_SQL = """
-- Sprint 2.1: Append-only op log tables (migration 065)
CREATE TABLE IF NOT EXISTS kg_entity_crdt (
    op_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id      INTEGER NOT NULL,
    agent_id       TEXT NOT NULL,
    op             TEXT NOT NULL CHECK (op IN ('add', 'remove')),
    version_vector TEXT NOT NULL,
    name           TEXT,
    entity_type    TEXT,
    description    TEXT,
    fingerprint    TEXT,
    timestamp      REAL NOT NULL,
    applied        INTEGER DEFAULT 0,
    tenant_id      TEXT DEFAULT 'default'
);

CREATE INDEX IF NOT EXISTS idx_kg_entity_crdt_entity ON kg_entity_crdt(entity_id);
CREATE INDEX IF NOT EXISTS idx_kg_entity_crdt_agent ON kg_entity_crdt(agent_id);
CREATE INDEX IF NOT EXISTS idx_kg_entity_crdt_ts ON kg_entity_crdt(timestamp);
CREATE INDEX IF NOT EXISTS idx_kg_entity_crdt_tenant ON kg_entity_crdt(tenant_id);

CREATE TABLE IF NOT EXISTS kg_edge_crdt (
    op_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_id        INTEGER NOT NULL,
    source_id      INTEGER NOT NULL,
    target_id      INTEGER NOT NULL,
    relation       TEXT NOT NULL,
    weight         REAL NOT NULL DEFAULT 1.0,
    valid_at       TEXT,
    agent_id       TEXT NOT NULL,
    version_vector TEXT NOT NULL,
    timestamp      REAL NOT NULL,
    applied        INTEGER DEFAULT 0,
    tenant_id      TEXT DEFAULT 'default'
);

CREATE INDEX IF NOT EXISTS idx_kg_edge_crdt_edge ON kg_edge_crdt(edge_id);
CREATE INDEX IF NOT EXISTS idx_kg_edge_crdt_agent ON kg_edge_crdt(agent_id);
CREATE INDEX IF NOT EXISTS idx_kg_edge_crdt_ts ON kg_edge_crdt(timestamp);
CREATE INDEX IF NOT EXISTS idx_kg_edge_crdt_tenant ON kg_edge_crdt(tenant_id);
"""


def _ensure_kg_crdt_tenant_columns(conn: AnyConnection) -> None:
    """Add tenant_id (and applied) columns to the CRDT tables if missing.

    ``ensure_kg_crdt_schema`` uses ``CREATE TABLE IF NOT EXISTS``, which does
    not alter already-created tables. Databases created by earlier migrations
    (e.g. 021) have ``kg_entity_crdt`` / ``kg_edge_crdt`` without the
    ``tenant_id`` and ``applied`` columns that the append-only op-log design
    requires. Without this idempotent ``ALTER TABLE`` the sync server's
    tenant-scoped INSERT/SELECT and the applied-tracking UPDATE would fail
    with "no such column". Safe to call on every connection open.
    """
    for table in ("kg_entity_crdt", "kg_edge_crdt"):
        try:
            cols = {
                r[1]
                for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if "tenant_id" not in cols:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT DEFAULT 'default'"
                )
            if "applied" not in cols:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN applied INTEGER DEFAULT 0"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_applied ON {table}(applied)"
                )
            if table == "kg_entity_crdt" and "fingerprint" not in cols:
                # fingerprint was added to the append-only op-log design
                # (migration 065) but the live kg_entity_crdt table created
                # by migration 021 predates it. Align so record_entity_add
                # / compute_entity_crdt_state can store and read it.
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN fingerprint TEXT"
                )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("_ensure_kg_crdt_tenant_columns: %s.%s: %s", table, e, type(conn).__name__)


def ensure_kg_crdt_schema(conn: AnyConnection) -> None:
    """Create the CRDT tables if they don't exist.

    Idempotent — safe to call on every connection open.
    """
    conn.executescript(_KG_CRDT_SCHEMA_SQL)
    _ensure_kg_crdt_tenant_columns(conn)
    ensure_kg_entity_redirect_schema(conn)
    conn.commit()


def ensure_kg_entity_redirect_schema(conn: AnyConnection) -> None:
    """Create the durable entity redirect map table if it doesn't exist.

    Sprint 2.4: persists loser_id -> winner_id mappings so that
    name/fingerprint collisions resolved during projection remain
    resolvable across sessions.  Idempotent.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS kg_entity_redirect (
            loser_id    INTEGER NOT NULL,
            winner_id   INTEGER NOT NULL,
            reason      TEXT    DEFAULT 'collision',
            created_at  TEXT    DEFAULT (datetime('now')),
            tenant_id   TEXT    DEFAULT '',
            PRIMARY KEY (loser_id, winner_id)
        );
        CREATE INDEX IF NOT EXISTS idx_kg_entity_redirect_winner
            ON kg_entity_redirect(winner_id);
        CREATE INDEX IF NOT EXISTS idx_kg_entity_redirect_tenant
            ON kg_entity_redirect(tenant_id);
        """
    )


def resolve_entity_id(conn: AnyConnection, entity_id: int) -> int:
    """Resolve a possibly-stale entity_id to its current winner.

    If ``entity_id`` was merged away during a collision resolution,
    returns the live winner.  Returns ``entity_id`` unchanged when no
    redirect exists.
    """
    row = conn.execute(
        "SELECT winner_id FROM kg_entity_redirect WHERE loser_id=? ORDER BY rowid DESC LIMIT 1",
        (entity_id,),
    ).fetchone()
    return int(row[0]) if row else entity_id


def persist_entity_redirects(
    conn: AnyConnection,
    redirects: dict[int, int],
    tenant_id: str = "",
) -> None:
    """Persist a loser_id -> winner_id redirect map durably.

    Idempotent via the PRIMARY KEY; re-projection only widens the map.
    """
    if not redirects:
        return
    conn.executemany(
        """
        INSERT OR IGNORE INTO kg_entity_redirect (loser_id, winner_id, tenant_id)
        VALUES (?, ?, ?)
        """,
        [(loser, winner, tenant_id) for loser, winner in redirects.items()],
    )


# ---------------------------------------------------------------------------
# High-level helpers (the user-facing API)
# ---------------------------------------------------------------------------


def record_entity_add(
    conn: AnyConnection,
    entity_id: int,
    agent_id: str,
    version_vector: dict[str, int],
    name: str,
    entity_type: str = "",
    description: str = "",
    fingerprint: str | None = None,
    tenant_id: str = "default",
) -> None:
    """Record an "add" op for an entity. Append-only (Sprint 2.1)."""
    conn.execute(
        """
        INSERT INTO kg_entity_crdt
            (entity_id, agent_id, op, version_vector, name,
             entity_type, description, fingerprint, timestamp, tenant_id)
        VALUES (?, ?, 'add', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity_id,
            agent_id,
            json.dumps(version_vector, sort_keys=True),
            name,
            entity_type,
            description,
            fingerprint,
            time.time(),
            tenant_id,
        ),
    )


def record_entity_remove(
    conn: AnyConnection,
    entity_id: int,
    agent_id: str,
    version_vector: dict[str, int],
    tenant_id: str = "default",
) -> None:
    """Record a "remove" op for an entity. Append-only (Sprint 2.1)."""
    conn.execute(
        """
        INSERT INTO kg_entity_crdt
            (entity_id, agent_id, op, version_vector, name,
             entity_type, description, timestamp, tenant_id)
        VALUES (?, ?, 'remove', ?, '', '', '', ?, ?)
        """,
        (
            entity_id,
            agent_id,
            json.dumps(version_vector, sort_keys=True),
            time.time(),
            tenant_id,
        ),
    )


def record_edge_add(
    conn: AnyConnection,
    source_id: int,
    target_id: int,
    relation: str,
    weight: float,
    agent_id: str,
    version_vector: dict[str, int],
    valid_at: Optional[str] = None,
    tenant_id: str = "default",
) -> None:
    """Record an "add" op for an edge. Append-only (Sprint 2.1)."""
    edge_id = _edge_key(source_id, target_id, relation)
    conn.execute(
        """
        INSERT INTO kg_edge_crdt
            (edge_id, source_id, target_id, relation, weight,
             valid_at, agent_id, version_vector, timestamp, tenant_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            edge_id,
            source_id,
            target_id,
            relation,
            weight,
            valid_at,
            agent_id,
            json.dumps(version_vector, sort_keys=True),
            time.time(),
            tenant_id,
        ),
    )


# ---------------------------------------------------------------------------
# S2.7 (2026-06-23): name-collision handling
# ---------------------------------------------------------------------------
# When two peers create entities with the same (name, entity_type)
# but different CRDT entity_ids, the projection to kg_entities would
# hit the UNIQUE(name, entity_type) constraint. The CRDT layer treats
# each entity_id as a separate identity, but the kg_entities table
# treats (name, entity_type) as the natural key. We need to bridge
# these two views.
#
# The approach: at projection time, group entities by
# (name, entity_type) and pick the LWW winner. The losers are
# marked with the winner's entity_id so that any edges pointing to
# them get redirected.


def entity_dedup_via_crdt(
    state: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Resolve name-collisions in a merged entity state.

    Sprint 2.2: Uses inception fingerprint for dedup when available,
    falling back to (name, entity_type) for backward compatibility.

    The CRDT merge produces a dict keyed by entity_id. If two
    different entity_ids resolve to the same fingerprint or
    (name, entity_type), the UNIQUE constraint on kg_entities
    will reject the second. This helper picks the LWW winner
    and returns a "redirect map": the losing entity_ids are
    mapped to the winning entity_id.

    Returns:
      ``{"merged_state": <state with only winners>,
        "redirects": {loser_id: winner_id, ...}}``

    The caller should:
      1. Apply ``merged_state`` to kg_entities.
      2. Update any kg_edges.source_id / target_id that match a
         loser to use the winner's id.
    """
    # Sprint 2.2: Group by fingerprint first, then by (name, entity_type)
    by_fingerprint: dict[str, list[int]] = {}
    by_key: dict[tuple[str, str], list[int]] = {}
    for entity_id, info in state.items():
        if info.get("tombstone"):
            continue
        fp = info.get("fingerprint")
        if fp:
            by_fingerprint.setdefault(fp, []).append(entity_id)
        else:
            key = (info["name"], info.get("entity_type", ""))
            by_key.setdefault(key, []).append(entity_id)

    merged_state: dict[int, dict[str, Any]] = {}
    redirects: dict[int, int] = {}

    # Merge by fingerprint (same entity across peers)
    for fp, ids in by_fingerprint.items():
        if len(ids) == 1:
            merged_state[ids[0]] = state[ids[0]]
            continue
        # Multiple entity_ids with same fingerprint - LWW winner
        winner_id = max(ids)
        merged_state[winner_id] = state[winner_id]
        for loser_id in ids:
            if loser_id != winner_id:
                redirects[loser_id] = winner_id

    # Merge by (name, entity_type) for entities without fingerprint
    for _key, ids in by_key.items():
        if len(ids) == 1:
            merged_state[ids[0]] = state[ids[0]]
            continue
        # Multiple entity_ids collide. Pick the LWW winner.
        winner_id = max(ids)
        merged_state[winner_id] = state[winner_id]
        for loser_id in ids:
            if loser_id != winner_id:
                redirects[loser_id] = winner_id

    return {
        "merged_state": merged_state,
        "redirects": redirects,
    }


def redirect_edge_ids(
    state: dict[int, dict[str, Any]],
    redirects: dict[int, int],
) -> dict[int, dict[str, Any]]:
    """Update edge state to use redirected entity IDs.

    For each edge in ``state``, if source_id or target_id is in
    ``redirects``, replace it with the winner id. This must be
    called after ``entity_dedup_via_crdt`` and before applying the
    edge state to the kg_edges table.
    """
    new_state: dict[int, dict[str, Any]] = {}
    for edge_id, info in state.items():
        new_info = dict(info)
        if new_info["source_id"] in redirects:
            new_info["source_id"] = redirects[new_info["source_id"]]
        if new_info["target_id"] in redirects:
            new_info["target_id"] = redirects[new_info["target_id"]]
        new_state[edge_id] = new_info
    return new_state


# ---------------------------------------------------------------------------
# S2.10 (2026-06-23): unified dedup-and-project
# ---------------------------------------------------------------------------


def project_crdt_to_entities(
    conn: AnyConnection,
) -> tuple[int, int, dict[int, int]]:
    """Project the merged CRDT state into the kg_entities table.

    Steps:
      1. Read all entity ops, merge to canonical state.
      2. Resolve name-collisions (S2.7).
      3. Apply to kg_entities.
      4. Read all edge ops, redirect edge IDs through the
         collision-resolution map, merge, apply to kg_edges.

    Returns:
      (entities_written, edges_written, redirects_map)
    """
    entity_state = compute_entity_crdt_state(conn)
    dedup = entity_dedup_via_crdt(entity_state)
    merged_entities = dedup["merged_state"]
    redirects = dedup["redirects"]

    n_entities = apply_entity_crdt_to_db(conn, merged_entities)

    edge_state = compute_edge_crdt_state(conn)
    if redirects:
        edge_state = redirect_edge_ids(edge_state, redirects)

    # Sprint 2.4: persist the redirect map durably so loser->winner
    # resolution survives across projection runs (and outside the
    # projection path, e.g. external edge lookups).
    persist_entity_redirects(conn, redirects)
    n_edges = apply_edge_crdt_to_db(conn, edge_state)

    return n_entities, n_edges, redirects
