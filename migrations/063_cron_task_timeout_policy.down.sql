-- 063 down: Remove the cron_task_timeouts table.
-- All timeout behaviour falls back to MEMORY_WORKER_TASK_TIMEOUT_S env var
-- (default 120s), which is backward-compatible with pre-063 behaviour.

DROP TABLE IF EXISTS cron_task_timeouts;
