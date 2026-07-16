-- 063: Per-task-type timeout policy for cron background tasks.
--
-- Previously all tasks shared MEMORY_WORKER_TASK_TIMEOUT_S (default 120s)
-- regardless of their actual workload.  This table allows fine-grained
-- timeout, retry, and auto-retry configuration per task type.
--
-- The table is created with sensible defaults: most cron_* task types get
-- timeout_s=300, max_attempts=3, auto_retry_after_s=900 (15 min exponential
-- backoff).  Direct-script jobs (health_check, daemon_watchdog, etc.) get
-- shorter timeouts matching their jobs.py values.

CREATE TABLE IF NOT EXISTS cron_task_timeouts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type       TEXT NOT NULL UNIQUE,
    timeout_s       INTEGER NOT NULL DEFAULT 300,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    auto_retry_after_s INTEGER NOT NULL DEFAULT 900,
    auto_retry_max_extra INTEGER NOT NULL DEFAULT 3,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_cron_task_timeouts_type ON cron_task_timeouts(task_type);

-- Backfill with defaults for all known cron task types.
-- Most cron_* tasks: 300s timeout, 3 attempts, auto-retry after 15 min.

INSERT OR IGNORE INTO cron_task_timeouts(task_type, timeout_s, max_attempts, auto_retry_after_s)
VALUES
    ('cron_daily_digest', 300, 3, 900),
    ('cron_purge_auto_saves', 120, 3, 900),
    ('cron_integrity_check', 300, 3, 900),
    ('cron_log_retention', 120, 3, 900),
    ('cron_backfill_all', 600, 2, 1800),
    ('cron_backup', 300, 3, 900),
    ('cron_backup_validate', 120, 3, 900),
    ('cron_sync', 120, 3, 900),
    ('cron_crdt_sync', 120, 3, 900),
    ('cron_monitor_task_queue', 120, 3, 900),
    ('cron_cleanup_auto_logs', 120, 3, 900),
    ('cron_kg_backfill_monitor', 120, 3, 900),
    ('cron_embedding_recompute', 300, 3, 900),
    ('cron_detect_vec_drift', 120, 3, 900),
    ('cron_rewrite_links', 120, 3, 900),
    ('cron_consolidate', 300, 3, 900),
    ('cron_compact', 600, 2, 1800),
    ('cron_rebuild_fts', 300, 3, 900),
    ('cron_heartbeat', 300, 3, 900),
    ('cron_tier_migration', 300, 3, 900),
    ('cron_kg_backfill', 600, 2, 1800),
    ('cron_skill_extraction', 300, 3, 900),
    ('cron_cross_session_learn', 300, 3, 900),
    ('cron_pinned_decay', 120, 3, 900),
    ('cron_concept_drift', 300, 3, 900),
    ('cron_purge_expired', 120, 3, 900),
    ('cron_quality_filter', 120, 3, 900),
    ('cron_auto_summarize', 300, 3, 900),
    ('cron_retention_stats', 120, 3, 900),
    ('cron_auto_share', 120, 3, 900),
    ('cron_promote_drafts', 120, 3, 900),
    ('cron_semantic_clusters', 300, 3, 900),
    ('cron_skill_decay', 120, 3, 900),
    ('cron_review_beliefs', 300, 3, 900),
    ('cron_health_check', 120, 3, 900),
    ('cron_daemon_watchdog', 30, 3, 900),
    ('cron_watchdog', 60, 3, 900),
    ('cron_policy_hash_status', 60, 3, 900),
    ('cron_check_config_drift', 120, 3, 900),
    ('cron_train_forget_model', 300, 2, 3600),
    ('cron_train_temporal_ssm', 300, 2, 3600),
    ('cron_train_ltr', 600, 2, 3600),
    ('cron_pipeline_coverage', 60, 3, 900),
    ('cron_retry_dead_tasks', 120, 3, 900),

    -- Non-cron task types (enqueued by save pipeline)
    ('entity_resolution', 300, 3, 900),
    ('fact_consolidation', 300, 3, 900),
    ('semantic_backlinks', 120, 3, 900),
    ('wal_checkpoint', 60, 3, 900),
    ('chunk_embedding_index', 120, 3, 900),
    ('colbert_index', 120, 3, 900),
    ('splade_index', 120, 3, 900),
    ('embedding_index', 120, 3, 900),
    ('kg_and_fact_index', 300, 3, 900),
    ('entailment_chains', 300, 3, 900),
    ('concept_compilation', 300, 3, 900),
    ('skill_enrichment', 300, 3, 900),
    ('consistency_check', 120, 3, 900),
    ('revalidate_entailments', 300, 3, 900),
    ('contradiction_check', 300, 3, 900),
    ('run_script', 300, 3, 900);
