-- Migration 021: S2 (2026-06-23) — Graph CRDTs for peer-to-peer KG replication
--
-- Background
-- ----------
-- The pre-S2 system used a Last-Writer-Wins (LWW) approach for the
-- kg_entities and kg_edges tables, which is NOT a proper CRDT. In
-- multi-peer setups (laptop + desktop), this caused:
--   * Silent data loss on concurrent updates
--   * Duplicate entities when names collided across peers
--   * Inconsistent edge sets
--
-- The S2 design introduces two new tables that hold per-peer
-- operations with version vectors, enabling proper CRDT merges
-- (commutative, associative, idempotent, convergent).
--
-- New tables
-- ----------
--   kg_entity_crdt: per-peer add/remove ops for entities, with
--                    version vectors for causal ordering. 2P-Set
--                    semantics: add wins on concurrent add/remove.
--                    LWW per metadata field (name, entity_type,
--                    description).
--
--   kg_edge_crdt:   per-peer add ops for edges with LWW metadata.
--                    Edges are add-only (deletion is represented
--                    by setting invalid_at on the kg_edges row).
--                    The edge_id is a stable hash of
--                    (source_id, target_id, relation) so peers
--                    agree on edge identity.
--
-- Idempotency
-- -----------
-- All statements use IF NOT EXISTS / OR REPLACE so this migration
-- is safe to run on an already-migrated DB.
--
-- Backward compat
-- ---------------
-- The original kg_entities and kg_edges tables are unchanged. The
-- CRDT tables are additive — sync_server.py will populate them on
-- first run via ``record_entity_add`` / ``record_edge_add``.

CREATE TABLE IF NOT EXISTS kg_entity_crdt (
    entity_id      INTEGER PRIMARY KEY,
    agent_id       TEXT    NOT NULL,
    op             TEXT    NOT NULL CHECK (op IN ('add', 'remove')),
    version_vector TEXT    NOT NULL,
    name           TEXT,
    entity_type    TEXT,
    description    TEXT,
    timestamp      REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kg_entity_crdt_agent
    ON kg_entity_crdt(agent_id);
CREATE INDEX IF NOT EXISTS idx_kg_entity_crdt_ts
    ON kg_entity_crdt(timestamp);

CREATE TABLE IF NOT EXISTS kg_edge_crdt (
    edge_id        INTEGER PRIMARY KEY,
    source_id      INTEGER NOT NULL,
    target_id      INTEGER NOT NULL,
    relation       TEXT    NOT NULL,
    weight         REAL    NOT NULL DEFAULT 1.0,
    valid_at       TEXT,
    agent_id       TEXT    NOT NULL,
    version_vector TEXT    NOT NULL,
    timestamp      REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kg_edge_crdt_agent
    ON kg_edge_crdt(agent_id);
CREATE INDEX IF NOT EXISTS idx_kg_edge_crdt_ts
    ON kg_edge_crdt(timestamp);
CREATE INDEX IF NOT EXISTS idx_kg_edge_crdt_pair
    ON kg_edge_crdt(source_id, target_id, relation);
