-- Migration 017 down: drop the CASCADE FK constraints added in
-- migrations/017_kg_cascade.sql.  Reverses kg_edges -> kg_entities
-- (back to no cascade) and backlinks -> memories (back to no FK).
--
-- Idempotent: IF EXISTS guards everywhere.

PRAGMA foreign_keys = OFF;

-- 1. Revert kg_edges to the pre-migration definition (no cascade).
CREATE TABLE IF NOT EXISTS kg_edges_orig (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES kg_entities(id),
    target_id INTEGER NOT NULL REFERENCES kg_entities(id),
    relation TEXT NOT NULL DEFAULT 'related_to',
    weight REAL DEFAULT 1.0,
    created_at TEXT,
    valid_at TEXT,
    invalid_at TEXT,
    UNIQUE(source_id, target_id, relation)
);

INSERT OR IGNORE INTO kg_edges_orig
    SELECT id, source_id, target_id, relation, weight, created_at,
           valid_at, invalid_at
    FROM kg_edges;

DROP TABLE kg_edges;
ALTER TABLE kg_edges_orig RENAME TO kg_edges;

CREATE INDEX IF NOT EXISTS idx_kg_edges_source ON kg_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_kg_edges_target ON kg_edges(target_id);
CREATE INDEX IF NOT EXISTS idx_kg_edges_relation ON kg_edges(relation);
CREATE INDEX IF NOT EXISTS idx_kg_edges_valid_at ON kg_edges(valid_at);
CREATE INDEX IF NOT EXISTS idx_kg_edges_invalid_at ON kg_edges(invalid_at);

-- 2. Revert backlinks to the pre-migration definition (no FK).
CREATE TABLE IF NOT EXISTS backlinks_orig (
    source_id TEXT,
    target_id TEXT,
    PRIMARY KEY (source_id, target_id)
);

INSERT OR IGNORE INTO backlinks_orig
    SELECT source_id, target_id FROM backlinks;

DROP TABLE backlinks;
ALTER TABLE backlinks_orig RENAME TO backlinks;

CREATE INDEX IF NOT EXISTS idx_backlinks_target_id ON backlinks(target_id);

PRAGMA foreign_keys = ON;
