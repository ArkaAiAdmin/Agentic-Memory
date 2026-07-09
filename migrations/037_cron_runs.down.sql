-- Migration 037 down: Remove cron execution tracking

DROP INDEX IF EXISTS idx_cron_runs_status;
DROP INDEX IF EXISTS idx_cron_runs_started;
DROP INDEX IF EXISTS idx_cron_runs_job;
DROP TABLE IF EXISTS cron_runs;
