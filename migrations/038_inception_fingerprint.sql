-- 038_inception_fingerprint: Add fingerprint + inception_at to kg_entities
-- Keeps UNIQUE(name, entity_type) for backwards compatibility.
-- The projection pipeline (crdt_projection.py) uses fingerprints for grouping;
-- the DB constraint is a safety net for direct inserts.
--
-- Production kg_entities schema (after migration 030):
--   id, name, entity_type, mentions, created_at, updated_at,
--   community_id, betweenness, UNIQUE(name, entity_type)
--
-- SQLite doesn't support ALTER ADD COLUMN with constraints, so recreate the table.

-- Step 1: Create new table with fingerprint column
CREATE TABLE kg_entities_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    entity_type  TEXT,
    mentions     INTEGER DEFAULT 1,
    created_at   TEXT,
    updated_at   TEXT,
    community_id INTEGER DEFAULT 0,
    betweenness  REAL    DEFAULT 0.0,
    fingerprint  TEXT,
    inception_at TEXT,
    UNIQUE(name, entity_type)
);

-- Step 2: Copy data (fingerprints computed by Python backfill script)
INSERT INTO kg_entities_new (id, name, entity_type, mentions, created_at, updated_at, community_id, betweenness)
SELECT id, name, entity_type, mentions, created_at, updated_at, community_id, betweenness
FROM kg_entities;

-- Step 3: Drop old table, rename new
DROP TABLE kg_entities;
ALTER TABLE kg_entities_new RENAME TO kg_entities;

-- Step 4: Recreate indexes
CREATE INDEX idx_kg_entities_name ON kg_entities(name);
CREATE INDEX idx_kg_entities_type ON kg_entities(entity_type);
CREATE INDEX idx_kg_entities_community_id ON kg_entities(community_id);
CREATE INDEX idx_kg_entities_betweenness ON kg_entities(betweenness);
CREATE INDEX idx_kg_entities_fingerprint ON kg_entities(fingerprint);
