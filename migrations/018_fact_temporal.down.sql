-- Migration 018 down: drop fact-level temporal validity columns
--
-- Reverses migrations/018_fact_temporal.sql.
--
-- Data preservation: kg_facts is renamed to kg_facts_pre_rollback_018
-- before recreation so existing rows survive the column drop. The
-- canonical columns (id, subject, predicate, object, confidence, locked,
-- first_seen, last_seen, mention_count, source_memory, context,
-- subject_entity_id, object_entity_id) survive; temporal columns are
-- derived and re-runnable on re-upgrade.
--
-- SQLite cannot DROP COLUMN without recreating the table when the column
-- has a FOREIGN KEY constraint, so we use the standard 12-step
-- recreation per https://www.sqlite.org/lang_altertable.html.  The
-- transaction wraps the whole recreation so a mid-migration crash
-- leaves the table untouched.

PRAGMA foreign_keys = OFF;

-- Step 1: rename existing data to a safe landing zone (M1 data-persistence)
ALTER TABLE kg_facts RENAME TO kg_facts_pre_rollback_018;

-- Step 2: create the pre-temporal-columns schema
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

-- Step 3: copy canonical columns (drop temporal columns)
INSERT OR IGNORE INTO kg_facts_new
    (id, subject, predicate, object, confidence, locked,
     first_seen, last_seen, mention_count, source_memory, context,
     subject_entity_id, object_entity_id)
SELECT
    id, subject, predicate, object, confidence, locked,
    first_seen, last_seen, mention_count, source_memory, context,
    subject_entity_id, object_entity_id
FROM kg_facts_pre_rollback_018;

-- Step 4: clean up old data table and rename new into place
DROP TABLE IF EXISTS kg_facts_pre_rollback_018;
DROP INDEX IF EXISTS idx_kg_facts_validity;
DROP INDEX IF EXISTS idx_kg_facts_superseded_by;
DROP INDEX IF EXISTS idx_kg_facts_event_time;
ALTER TABLE kg_facts_new RENAME TO kg_facts;

PRAGMA foreign_keys = ON;
