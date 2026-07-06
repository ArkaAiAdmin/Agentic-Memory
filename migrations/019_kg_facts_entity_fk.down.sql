-- Migration 019 down: drop ON DELETE SET NULL from kg_facts entity FKs
--
-- Reverses migrations/019_kg_facts_entity_fk.sql.
-- Reverts subject_entity_id and object_entity_id FKs back to plain
-- REFERENCES (no ON DELETE clause).
--
-- Idempotent: uses IF NOT EXISTS guards and INSERT OR IGNORE.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS kg_facts_orig (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    locked INTEGER DEFAULT 0,
    first_seen REAL,
    last_seen REAL,
    mention_count INTEGER DEFAULT 1,
    source_memory TEXT,
    context TEXT,
    subject_entity_id INTEGER REFERENCES kg_entities(id),
    object_entity_id INTEGER REFERENCES kg_entities(id),
    event_time REAL,
    event_time_granularity TEXT,
    transaction_time REAL,
    valid_at REAL,
    invalid_at REAL,
    superseded_by INTEGER REFERENCES kg_facts(id) ON DELETE SET NULL,
    supersedes INTEGER REFERENCES kg_facts(id) ON DELETE SET NULL,
    contradiction_score REAL DEFAULT 0.0,
    invalidation_reason TEXT,
    UNIQUE(subject, predicate, object),
    FOREIGN KEY (source_memory) REFERENCES memories(id) ON DELETE SET NULL
);

INSERT OR IGNORE INTO kg_facts_orig
    (id, subject, predicate, object, confidence, locked,
     first_seen, last_seen, mention_count, source_memory, context,
     subject_entity_id, object_entity_id,
     event_time, event_time_granularity, transaction_time,
     valid_at, invalid_at, superseded_by, supersedes,
     contradiction_score, invalidation_reason)
SELECT
    id, subject, predicate, object, confidence, locked,
    first_seen, last_seen, mention_count, source_memory, context,
    subject_entity_id, object_entity_id,
    event_time, event_time_granularity, transaction_time,
    valid_at, invalid_at, superseded_by, supersedes,
    contradiction_score, invalidation_reason
FROM kg_facts;

DROP INDEX IF EXISTS idx_kg_facts_subject_entity;
DROP INDEX IF EXISTS idx_kg_facts_object_entity;
DROP TABLE kg_facts;
ALTER TABLE kg_facts_orig RENAME TO kg_facts;

CREATE INDEX IF NOT EXISTS idx_kg_facts_subject ON kg_facts(subject);
CREATE INDEX IF NOT EXISTS idx_kg_facts_predicate ON kg_facts(predicate);
CREATE INDEX IF NOT EXISTS idx_kg_facts_object ON kg_facts(object);
CREATE INDEX IF NOT EXISTS idx_kg_facts_spo ON kg_facts(subject, predicate, object);
CREATE INDEX IF NOT EXISTS idx_kg_facts_subject_entity ON kg_facts(subject_entity_id);
CREATE INDEX IF NOT EXISTS idx_kg_facts_object_entity ON kg_facts(object_entity_id);
CREATE INDEX IF NOT EXISTS idx_kg_facts_validity ON kg_facts(valid_at, invalid_at);
CREATE INDEX IF NOT EXISTS idx_kg_facts_superseded_by ON kg_facts(superseded_by);
CREATE INDEX IF NOT EXISTS idx_kg_facts_event_time ON kg_facts(event_time);

PRAGMA foreign_keys = ON;
