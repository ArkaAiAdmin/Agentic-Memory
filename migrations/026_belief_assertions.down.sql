-- Down migration 026: drop belief_assertions table and fact_type column

-- Drop belief_assertions table (cascades to indexes)
DROP TABLE IF EXISTS belief_assertions;

-- Drop fact_type column from kg_facts via temp table recreate
CREATE TABLE kg_facts_temp AS SELECT
    id, subject, predicate, object, confidence, locked,
    first_seen, last_seen, mention_count, source_memory, context,
    subject_entity_id, object_entity_id,
    event_time, event_time_granularity, transaction_time,
    valid_at, invalid_at, superseded_by, supersedes,
    contradiction_score, invalidation_reason,
    belief_status, epistemic_source, asserting_agent_id,
    evidence_chain, embedding
FROM kg_facts;

DROP TABLE kg_facts;
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
FROM kg_facts_temp;

DROP TABLE kg_facts_temp;

-- Recreate indexes
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
