-- Migration 000: Base schema (memories table + KG tables)
-- This is the new baseline migration. It replaces the inline
-- CREATE TABLE blocks that were previously in migration_runner.py.
-- Idempotent: IF NOT EXISTS guards allow safe re-runs on existing
-- databases that already have these tables.

CREATE TABLE IF NOT EXISTS memories (
    id                  TEXT PRIMARY KEY,
    content             TEXT NOT NULL,
    source_file         TEXT NOT NULL,
    tags                TEXT DEFAULT '[]',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    observed_at         TEXT NOT NULL,
    pinned              INTEGER DEFAULT 0,
    importance          INTEGER DEFAULT 3,
    decay               TEXT DEFAULT 'none',
    score               REAL DEFAULT 1.0,
    supersedes          TEXT,
    repo_id             TEXT,
    access_count        INTEGER DEFAULT 1,
    success_score       REAL DEFAULT 0.0,
    fitness_score       REAL DEFAULT 1.0,
    conflict_policy     TEXT DEFAULT 'supersede',
    version_vector      TEXT DEFAULT '{}',
    logical_clock       INTEGER DEFAULT 0,
    consolidation_state TEXT DEFAULT 'working'
);

CREATE TABLE IF NOT EXISTS kg_entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    entity_type TEXT,
    mentions    INTEGER DEFAULT 1,
    created_at  TEXT,
    updated_at  TEXT,
    UNIQUE(name, entity_type)
);

CREATE INDEX IF NOT EXISTS idx_kg_entities_name ON kg_entities(name);
CREATE INDEX IF NOT EXISTS idx_kg_entities_type ON kg_entities(entity_type);

CREATE TABLE IF NOT EXISTS kg_edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
    target_id   INTEGER NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
    relation    TEXT NOT NULL DEFAULT 'related_to',
    weight      REAL DEFAULT 1.0,
    created_at  TEXT,
    valid_at    TEXT,
    invalid_at  TEXT,
    UNIQUE(source_id, target_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_kg_edges_source ON kg_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_kg_edges_target ON kg_edges(target_id);
CREATE INDEX IF NOT EXISTS idx_kg_edges_relation ON kg_edges(relation);
CREATE INDEX IF NOT EXISTS idx_kg_edges_valid_at ON kg_edges(valid_at);
CREATE INDEX IF NOT EXISTS idx_kg_edges_invalid_at ON kg_edges(invalid_at);

CREATE TABLE IF NOT EXISTS kg_facts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    subject             TEXT NOT NULL,
    predicate           TEXT NOT NULL,
    object              TEXT NOT NULL,
    confidence          REAL DEFAULT 1.0,
    locked              INTEGER DEFAULT 0,
    first_seen          REAL,
    last_seen           REAL,
    mention_count       INTEGER DEFAULT 1,
    source_memory       TEXT,
    context             TEXT,
    UNIQUE(subject, predicate, object)
);

CREATE INDEX IF NOT EXISTS idx_kg_facts_subject ON kg_facts(subject);
CREATE INDEX IF NOT EXISTS idx_kg_facts_predicate ON kg_facts(predicate);
CREATE INDEX IF NOT EXISTS idx_kg_facts_object ON kg_facts(object);
CREATE INDEX IF NOT EXISTS idx_kg_facts_spo ON kg_facts(subject, predicate, object);

CREATE VIRTUAL TABLE kg_entities_fts USING fts5(
    name, entity_type, content='kg_entities', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS kg_entities_fts_ai AFTER INSERT ON kg_entities BEGIN
    INSERT INTO kg_entities_fts(rowid, name, entity_type)
    VALUES (new.id, new.name, new.entity_type); END;
CREATE TRIGGER IF NOT EXISTS kg_entities_fts_ad AFTER DELETE ON kg_entities BEGIN
    INSERT INTO kg_entities_fts(kg_entities_fts, rowid, name, entity_type)
    VALUES ('delete', old.id, old.name, old.entity_type); END;
CREATE TRIGGER IF NOT EXISTS kg_entities_fts_au AFTER UPDATE ON kg_entities BEGIN
    INSERT INTO kg_entities_fts(kg_entities_fts, rowid, name, entity_type)
    VALUES ('delete', old.id, old.name, old.entity_type);
    INSERT INTO kg_entities_fts(rowid, name, entity_type)
    VALUES (new.id, new.name, new.entity_type); END;

INSERT INTO kg_entities_fts(rowid, name, entity_type)
SELECT id, name, entity_type FROM kg_entities
WHERE NOT EXISTS (SELECT 1 FROM kg_entities_fts);
