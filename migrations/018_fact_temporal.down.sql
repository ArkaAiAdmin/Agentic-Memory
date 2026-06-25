-- Migration 018 down: drop fact-level temporal validity columns
--
-- Reverses migrations/018_fact_temporal.sql.
--
-- SQLite cannot DROP COLUMN without recreating the table when the column
-- has a FOREIGN KEY constraint, so we use the standard 12-step
-- recreation per https://www.sqlite.org/lang_altertable.html.  The
-- transaction wraps the whole recreation so a mid-migration crash
-- leaves the table untouched.
--
-- We DO NOT preserve transaction_time, event_time, etc. — they're
-- derived data and the backfill is re-runnable.  Only the canonical
-- columns (id, subject, predicate, object, confidence, locked,
-- first_seen, last_seen, mention_count, source_memory, context,
-- subject_entity_id, object_entity_id) survive.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS kg_facts_new (
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
    UNIQUE(subject, predicate, object),
    FOREIGN KEY (source_memory) REFERENCES memories(id) ON DELETE SET NULL
);

INSERT OR IGNORE INTO kg_facts_new
    (id, subject, predicate, object, confidence, locked,
     first_seen, last_seen, mention_count, source_memory, context,
     subject_entity_id, object_entity_id)
SELECT
    id, subject, predicate, object, confidence, locked,
    first_seen, last_seen, mention_count, source_memory, context,
    subject_entity_id, object_entity_id
FROM kg_facts;

DROP INDEX IF EXISTS idx_kg_facts_validity;
DROP INDEX IF EXISTS idx_kg_facts_superseded_by;
DROP INDEX IF EXISTS idx_kg_facts_event_time;
DROP TABLE kg_facts;
ALTER TABLE kg_facts_new RENAME TO kg_facts;

PRAGMA foreign_keys = ON;
