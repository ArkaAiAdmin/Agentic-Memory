-- Down migration 026: drop belief_assertions table and fact_type column
--
-- Data preservation: kg_facts is renamed to kg_facts_pre_rollback_026
-- before recreation, so all rows survive the fact_type column drop.
--
-- kg_facts_fts (FTS5 virtual table, created by 020) is dropped before
-- kg_facts is recreated and recreated after to keep the virtual table
-- in sync.  Foreign keys are disabled for the recreation period.

PRAGMA foreign_keys = OFF;

-- Drop kg_facts_fts (FTS5 virtual table) and its sync triggers so
-- recreating kg_facts does not leave a stale FTS5 attachment.
-- These may be absent if earlier down migrations already removed them;
-- IF EXISTS guards handle that idempotently.
DROP TRIGGER IF EXISTS kg_facts_fts_ai;
DROP TRIGGER IF EXISTS kg_facts_fts_ad;
DROP TRIGGER IF EXISTS kg_facts_fts_au;
DROP TABLE IF EXISTS kg_facts_fts;

-- Drop belief_assertions table (cascades to indexes)
DROP TABLE IF EXISTS belief_assertions;

-- Data-safe kg_facts column removal: rename first so rows survive.
ALTER TABLE kg_facts RENAME TO kg_facts_pre_rollback_026;

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
    belief_status TEXT DEFAULT 'active',
    epistemic_source TEXT DEFAULT 'agent',
    asserting_agent_id TEXT,
    evidence_chain TEXT,
    embedding BLOB,
    UNIQUE(subject, predicate, object)
);

-- Bulk copy all surviving columns (fact_type excluded; it was the only
-- column added by 026; everything else was already present before).
INSERT INTO kg_facts (
    id, subject, predicate, object, confidence, locked,
    first_seen, last_seen, mention_count, source_memory, context,
    subject_entity_id, object_entity_id,
    event_time, event_time_granularity, transaction_time,
    valid_at, invalid_at, superseded_by, supersedes,
    contradiction_score, invalidation_reason,
    belief_status, epistemic_source, asserting_agent_id,
    evidence_chain, embedding
) SELECT
    id, subject, predicate, object, confidence, locked,
    first_seen, last_seen, mention_count, source_memory, context,
    subject_entity_id, object_entity_id,
    event_time, event_time_granularity, transaction_time,
    valid_at, invalid_at, superseded_by, supersedes,
    contradiction_score, invalidation_reason,
    belief_status, epistemic_source, asserting_agent_id,
    evidence_chain, embedding
FROM kg_facts_pre_rollback_026;

DROP TABLE IF EXISTS kg_facts_pre_rollback_026;

-- Recreate indexes removed in 026 up
CREATE INDEX IF NOT EXISTS idx_kg_facts_subject ON kg_facts(subject);
CREATE INDEX IF NOT EXISTS idx_kg_facts_predicate ON kg_facts(predicate);
CREATE INDEX IF NOT EXISTS idx_kg_facts_object ON kg_facts(object);
CREATE INDEX IF NOT EXISTS idx_kg_facts_spo ON kg_facts(subject, predicate, object);
CREATE INDEX IF NOT EXISTS idx_kg_facts_subject_entity ON kg_facts(subject_entity_id);
CREATE INDEX IF NOT EXISTS idx_kg_facts_object_entity ON kg_facts(object_entity_id);
CREATE INDEX IF NOT EXISTS idx_kg_facts_validity ON kg_facts(valid_at, invalid_at);
CREATE INDEX IF NOT EXISTS idx_kg_facts_superseded_by ON kg_facts(superseded_by);
CREATE INDEX IF NOT EXISTS idx_kg_facts_event_time ON kg_facts(event_time);
CREATE INDEX IF NOT EXISTS idx_kg_facts_belief_status ON kg_facts(belief_status);
CREATE INDEX IF NOT EXISTS idx_kg_facts_epistemic_source ON kg_facts(epistemic_source);

-- Recreate kg_facts_fts (FTS5 virtual table) so subsequent down migrations
-- or re-runs of 020 down can cleanly drop it.
CREATE VIRTUAL TABLE IF NOT EXISTS kg_facts_fts USING fts5(
    subject, predicate, object, context,
    content='kg_facts', content_rowid='id'
);

-- Recreate the sync triggers so FTS stays in lockstep with kg_facts
CREATE TRIGGER IF NOT EXISTS kg_facts_fts_ai AFTER INSERT ON kg_facts BEGIN
    INSERT INTO kg_facts_fts(rowid, subject, predicate, object, context)
    VALUES (new.id, new.subject, new.predicate, new.object, new.context);
END;

CREATE TRIGGER IF NOT EXISTS kg_facts_fts_ad AFTER DELETE ON kg_facts BEGIN
    INSERT INTO kg_facts_fts(kg_facts_fts, rowid, subject, predicate, object, context)
    VALUES ('delete', old.id, old.subject, old.predicate, old.object, old.context);
END;

CREATE TRIGGER IF NOT EXISTS kg_facts_fts_au AFTER UPDATE ON kg_facts BEGIN
    INSERT INTO kg_facts_fts(kg_facts_fts, rowid, subject, predicate, object, context)
    VALUES ('delete', old.id, old.subject, old.predicate, old.object, old.context);
    INSERT INTO kg_facts_fts(rowid, subject, predicate, object, context)
    VALUES (new.id, new.subject, new.predicate, new.object, new.context);
END;

-- Backfill existing rows into kg_facts_fts so the FTS index stays in sync
INSERT INTO kg_facts_fts(kg_facts_fts) VALUES('rebuild');

PRAGMA foreign_keys = ON;
