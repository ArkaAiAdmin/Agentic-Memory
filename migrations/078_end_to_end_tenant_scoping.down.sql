-- Migration 078 down migration: Drop tenant indexes created for end-to-end tenant scoping.

DROP INDEX IF EXISTS idx_sessions_tenant;
DROP INDEX IF EXISTS idx_decision_threads_tenant;
DROP INDEX IF EXISTS idx_thread_events_tenant;
DROP INDEX IF EXISTS idx_session_compaction_log_tenant;
DROP INDEX IF EXISTS idx_belief_assertions_tenant;
DROP INDEX IF EXISTS idx_entailment_chains_tenant;
DROP INDEX IF EXISTS idx_graph_snapshots_tenant;
DROP INDEX IF EXISTS idx_belief_review_queue_tenant;
DROP INDEX IF EXISTS idx_memory_chunks_tenant;
DROP INDEX IF EXISTS idx_memory_embeddings_tenant;
DROP INDEX IF EXISTS idx_memory_vec_keys_tenant;
DROP INDEX IF EXISTS idx_colbert_tokens_tenant;
DROP INDEX IF EXISTS idx_splade_tokens_tenant;
DROP INDEX IF EXISTS idx_backlinks_tenant;
DROP INDEX IF EXISTS idx_user_access_log_tenant;
DROP INDEX IF EXISTS idx_user_profile_access_log_tenant;
DROP INDEX IF EXISTS idx_review_schedule_tenant;
DROP INDEX IF EXISTS idx_drift_alarms_tenant;
DROP INDEX IF EXISTS idx_concept_drift_tenant;
DROP INDEX IF EXISTS idx_cron_runs_tenant;
DROP INDEX IF EXISTS idx_coordination_audit_tenant;
DROP INDEX IF EXISTS idx_shared_tasks_tenant;
DROP INDEX IF EXISTS idx_agent_messages_tenant;

-- Restore pre-078 kg_entities table (drop composite UNIQUE constraint)
DROP INDEX IF EXISTS idx_kg_entities_tenant;
CREATE TABLE IF NOT EXISTS kg_entities_pre078 (
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
    UNIQUE(name, entity_type)
);

INSERT OR IGNORE INTO kg_entities_pre078 (id, name, entity_type, mentions, created_at, updated_at, community_id, betweenness, fingerprint, inception_at, tenant_id)
SELECT id, name, entity_type, mentions, created_at, updated_at, community_id, betweenness, fingerprint, inception_at, tenant_id
FROM kg_entities;

DROP TABLE kg_entities;
ALTER TABLE kg_entities_pre078 RENAME TO kg_entities;
CREATE INDEX IF NOT EXISTS idx_kg_entities_name ON kg_entities(name);
CREATE INDEX IF NOT EXISTS idx_kg_entities_type ON kg_entities(entity_type);

