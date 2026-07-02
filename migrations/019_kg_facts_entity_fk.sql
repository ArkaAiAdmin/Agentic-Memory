-- Migration 019: kg_facts ON DELETE SET NULL for entity FKs
--
-- 2026-06-23 (pre-existing bug fix): kg_facts.subject_entity_id and
-- kg_facts.object_entity_id are FKs to kg_entities(id) but have no
-- ON DELETE clause. When kg_dedup.merge_entities() tries to delete a
-- merged entity, the FK constraint fails (sqlite returns
-- "FOREIGN KEY constraint failed"). This bug was latent for a long
-- time — it was masked by the rare intersection of:
--   1. The dedup actually tries to DELETE the entity
--   2. There's a kg_facts row referencing it via subject/object_entity_id
--
-- Background worker was failing every 5 minutes with this error
-- (24 occurrences in worker.log) until the fix.
--
-- Fix: add ON DELETE SET NULL to both FKs. SQLite requires recreating
-- the table to change FK clauses (per the SQLite docs on ALTER TABLE,
-- which silently ignores attempts to add FK clauses), so we use the
-- standard 12-step recreation pattern (backup, drop, recreate,
-- copy).
--
-- This migration is idempotent: the recreation uses the same final
-- schema (with the FK fix), so running it on an already-migrated DB
-- is a no-op for the data. The only change is the FK clause.
--
-- Fresh-DB safety: on a brand-new database, kg_facts may not exist yet
-- (migration 018 adds temporal columns but does not create the table;
-- that responsibility belongs here).  The backup/copy/restore steps
-- gracefully no-op when the source table is absent — the migration
-- runner swallows the resulting "no such table" error (logged at debug
-- level) and proceeds to create the table fresh.

-- ---------------------------------------------------------------------------
-- 1. Back up existing kg_facts
-- ---------------------------------------------------------------------------

-- Create an empty backup table with the full schema first, then copy
-- data only if kg_facts already has rows.  This avoids "no such table"
-- on fresh DBs where kg_facts hasn't been created yet.
CREATE TABLE IF NOT EXISTS kg_facts_backup_019 (
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
    subject_entity_id INTEGER REFERENCES kg_entities(id) ON DELETE SET NULL,
    object_entity_id INTEGER REFERENCES kg_entities(id) ON DELETE SET NULL,
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
-- Copy existing rows (no-op on fresh DBs where kg_facts doesn't exist yet;
-- the migration runner logs a debug-level message and continues).
-- Explicit column list: tolerant of kg_facts having extra unknowns
-- columns (e.g. from re-running on a DB already upgraded past 025).
INSERT INTO kg_facts_backup_019 (
    id, subject, predicate, object, confidence, locked,
    first_seen, last_seen, mention_count, source_memory, context,
    subject_entity_id, object_entity_id,
    event_time, event_time_granularity,
    transaction_time, valid_at, invalid_at,
    superseded_by, supersedes, contradiction_score, invalidation_reason
)
SELECT
    id, subject, predicate, object, confidence, locked,
    first_seen, last_seen, mention_count, source_memory, context,
    subject_entity_id, object_entity_id,
    event_time, event_time_granularity,
    transaction_time, valid_at, invalid_at,
    superseded_by, supersedes, contradiction_score, invalidation_reason
FROM kg_facts;

-- ---------------------------------------------------------------------------
-- 2. Drop the old table
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS kg_facts;

-- ---------------------------------------------------------------------------
-- 3. Recreate with ON DELETE SET NULL on the entity FKs
-- ---------------------------------------------------------------------------

CREATE TABLE kg_facts (
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
    subject_entity_id INTEGER REFERENCES kg_entities(id) ON DELETE SET NULL,
    object_entity_id INTEGER REFERENCES kg_entities(id) ON DELETE SET NULL,
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

-- ---------------------------------------------------------------------------
-- 4. Restore the data
-- ---------------------------------------------------------------------------

INSERT INTO kg_facts SELECT * FROM kg_facts_backup_019;

-- ---------------------------------------------------------------------------
-- 5. Recreate the indexes (autoindex 1 covers the UNIQUE constraint)
-- ---------------------------------------------------------------------------

CREATE INDEX idx_kg_facts_subject ON kg_facts(subject);
CREATE INDEX idx_kg_facts_predicate ON kg_facts(predicate);
CREATE INDEX idx_kg_facts_object ON kg_facts(object);
CREATE INDEX idx_kg_facts_spo ON kg_facts(subject, predicate, object);
CREATE INDEX idx_kg_facts_subject_entity ON kg_facts(subject_entity_id);
CREATE INDEX idx_kg_facts_object_entity ON kg_facts(object_entity_id);
CREATE INDEX idx_kg_facts_validity ON kg_facts(valid_at, invalid_at);
CREATE INDEX idx_kg_facts_superseded_by ON kg_facts(superseded_by);
CREATE INDEX idx_kg_facts_event_time ON kg_facts(event_time);

-- ---------------------------------------------------------------------------
-- 6. Drop the backup
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS kg_facts_backup_019;
