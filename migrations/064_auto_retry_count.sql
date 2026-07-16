-- 064: Add extra_retry_count column to task_queue for auto-retry tracking.
--
-- When a task exhausts its max_attempts and is permanently failed, the
-- auto-retry daemon (cron_retry_dead_tasks.py) can re-enqueue it up to
-- auto_retry_max_extra extra times, tracked by this column.
--
-- SQLite < 3.35 has no DROP COLUMN.  The column is additive and harmless
-- on rollback (unused column).

ALTER TABLE task_queue ADD COLUMN extra_retry_count INTEGER NOT NULL DEFAULT 0;
