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
