-- 041: Replace UNIQUE(name, entity_type) with UNIQUE(fingerprint) on kg_entities.
--
-- The projection pipeline groups by fingerprint (not name+type).
-- The DB constraint must match the pipeline's identity function.
-- Existing rows get fingerprints via backfill_fingerprints.py BEFORE this migration.
--
-- SQLite doesn't support ALTER CONSTRAINT, so recreate the table.

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
    UNIQUE(fingerprint)
);

INSERT INTO kg_entities_new (id, name, entity_type, mentions, created_at, updated_at, community_id, betweenness, fingerprint, inception_at)
SELECT id, name, entity_type, mentions, created_at, updated_at, community_id, betweenness, fingerprint, inception_at
FROM kg_entities;

DROP TABLE kg_entities;
ALTER TABLE kg_entities_new RENAME TO kg_entities;

CREATE INDEX idx_kg_entities_name ON kg_entities(name);
CREATE INDEX idx_kg_entities_type ON kg_entities(entity_type);
CREATE INDEX idx_kg_entities_community_id ON kg_entities(community_id);
CREATE INDEX idx_kg_entities_betweenness ON kg_entities(betweenness);
CREATE INDEX idx_kg_entities_fingerprint ON kg_entities(fingerprint);
