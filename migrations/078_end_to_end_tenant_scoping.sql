-- Migration 078: End-to-end tenant scoping across all remaining operational, session, belief, index, and coordination tables.
--
-- Prior migrations (042, 044, 050, 051, 055, 056, 075, 076) added tenant_id
-- to core tables (memories, kg_entities, kg_facts, kg_edges, memory_field_crdt,
-- memory_skills, file_locks, etc.).
--
-- Migration 078 completes end-to-end tenant isolation by adding tenant_id
-- to all remaining operational, index, session, and belief tables, along with
-- tenant indexes for fast scoped lookups.

-- Session & Decision System
ALTER TABLE sessions ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE decision_threads ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE thread_events ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE session_compaction_log ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_sessions_tenant ON sessions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_decision_threads_tenant ON decision_threads(tenant_id);
CREATE INDEX IF NOT EXISTS idx_thread_events_tenant ON thread_events(tenant_id);
CREATE INDEX IF NOT EXISTS idx_session_compaction_log_tenant ON session_compaction_log(tenant_id);

-- Belief & Reasoning System
ALTER TABLE belief_assertions ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE entailment_chains ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE graph_snapshots ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE belief_review_queue ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_belief_assertions_tenant ON belief_assertions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_entailment_chains_tenant ON entailment_chains(tenant_id);
CREATE INDEX IF NOT EXISTS idx_graph_snapshots_tenant ON graph_snapshots(tenant_id);
CREATE INDEX IF NOT EXISTS idx_belief_review_queue_tenant ON belief_review_queue(tenant_id);

-- Chunk & Indexing System
ALTER TABLE memory_chunks ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE memory_embeddings ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE memory_vec_keys ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE colbert_tokens ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE splade_tokens ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_memory_chunks_tenant ON memory_chunks(tenant_id);
CREATE INDEX IF NOT EXISTS idx_memory_embeddings_tenant ON memory_embeddings(tenant_id);
CREATE INDEX IF NOT EXISTS idx_memory_vec_keys_tenant ON memory_vec_keys(tenant_id);
CREATE INDEX IF NOT EXISTS idx_colbert_tokens_tenant ON colbert_tokens(tenant_id);
CREATE INDEX IF NOT EXISTS idx_splade_tokens_tenant ON splade_tokens(tenant_id);

-- Link & Audit Utilities
ALTER TABLE backlinks ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE user_access_log ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE user_profile_access_log ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE review_schedule ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE drift_alarms ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE concept_drift ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE cron_runs ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_backlinks_tenant ON backlinks(tenant_id);
CREATE INDEX IF NOT EXISTS idx_user_access_log_tenant ON user_access_log(tenant_id);
CREATE INDEX IF NOT EXISTS idx_user_profile_access_log_tenant ON user_profile_access_log(tenant_id);
CREATE INDEX IF NOT EXISTS idx_review_schedule_tenant ON review_schedule(tenant_id);
CREATE INDEX IF NOT EXISTS idx_drift_alarms_tenant ON drift_alarms(tenant_id);
CREATE INDEX IF NOT EXISTS idx_concept_drift_tenant ON concept_drift(tenant_id);
CREATE INDEX IF NOT EXISTS idx_cron_runs_tenant ON cron_runs(tenant_id);

-- Coordination System
ALTER TABLE coordination_audit ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE shared_tasks ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE agent_messages ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_coordination_audit_tenant ON coordination_audit(tenant_id);
CREATE INDEX IF NOT EXISTS idx_shared_tasks_tenant ON shared_tasks(tenant_id);
CREATE INDEX IF NOT EXISTS idx_agent_messages_tenant ON agent_messages(tenant_id);

-- KG Entities Multi-Tenant Fingerprint Uniqueness Migration
CREATE TABLE IF NOT EXISTS kg_entities_v78 (
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
    tenant_id    TEXT NOT NULL DEFAULT 'default',
    UNIQUE(tenant_id, fingerprint)
);

INSERT OR IGNORE INTO kg_entities_v78 (id, name, entity_type, mentions, created_at, updated_at, community_id, betweenness, fingerprint, inception_at, tenant_id)
SELECT id, name, entity_type, mentions, created_at, updated_at, community_id, betweenness, fingerprint, inception_at, tenant_id
FROM kg_entities;

DROP TABLE kg_entities;
ALTER TABLE kg_entities_v78 RENAME TO kg_entities;
CREATE INDEX IF NOT EXISTS idx_kg_entities_tenant ON kg_entities(tenant_id);
