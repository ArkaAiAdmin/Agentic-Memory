-- Migration 065: Make KG CRDT op log append-only
-- Sprint 2.1: The current kg_entity_crdt uses entity_id as PRIMARY KEY
-- with INSERT OR REPLACE, which overwrites previous ops. This migration
-- creates a new table with a composite key to preserve all ops.

-- Step 1: Create new append-only table
CREATE TABLE IF NOT EXISTS kg_entity_crdt_append (
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
    applied        INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_kg_entity_crdt_append_entity ON kg_entity_crdt_append(entity_id);
CREATE INDEX IF NOT EXISTS idx_kg_entity_crdt_append_agent ON kg_entity_crdt_append(agent_id);
CREATE INDEX IF NOT EXISTS idx_kg_entity_crdt_append_ts ON kg_entity_crdt_append(timestamp);
CREATE INDEX IF NOT EXISTS idx_kg_entity_crdt_append_applied ON kg_entity_crdt_append(applied);

-- Step 2: Migrate existing data (keep only latest per entity_id)
INSERT INTO kg_entity_crdt_append (entity_id, agent_id, op, version_vector, name, entity_type, description, timestamp)
SELECT entity_id, agent_id, op, version_vector, name, entity_type, description, timestamp
FROM kg_entity_crdt
WHERE (entity_id, timestamp) IN (
    SELECT entity_id, MAX(timestamp) FROM kg_entity_crdt GROUP BY entity_id
);

-- Step 3: Do NOT drop old table yet - keep for rollback safety
-- The old table will be dropped in a future migration after verification

-- Step 4: Create same structure for edge CRDT
CREATE TABLE IF NOT EXISTS kg_edge_crdt_append (
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
    applied        INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_kg_edge_crdt_append_edge ON kg_edge_crdt_append(edge_id);
CREATE INDEX IF NOT EXISTS idx_kg_edge_crdt_append_agent ON kg_edge_crdt_append(agent_id);
CREATE INDEX IF NOT EXISTS idx_kg_edge_crdt_append_ts ON kg_edge_crdt_append(timestamp);
CREATE INDEX IF NOT EXISTS idx_kg_edge_crdt_append_applied ON kg_edge_crdt_append(applied);

-- Step 5: Migrate existing edge data
INSERT INTO kg_edge_crdt_append (edge_id, source_id, target_id, relation, weight, valid_at, agent_id, version_vector, timestamp)
SELECT edge_id, source_id, target_id, relation, weight, valid_at, agent_id, version_vector, timestamp
FROM kg_edge_crdt
WHERE (edge_id, timestamp) IN (
    SELECT edge_id, MAX(timestamp) FROM kg_edge_crdt GROUP BY edge_id
);
