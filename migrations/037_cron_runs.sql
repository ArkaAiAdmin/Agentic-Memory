-- Migration 037: Cron execution tracking
-- Records every cron job execution for the consolidated scheduler
-- and the memory_system_health MCP tool.

CREATE TABLE IF NOT EXISTS cron_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name     TEXT NOT NULL,
    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    status       TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed', 'skipped')),
    duration_ms  INTEGER,
    error        TEXT,
    output       TEXT  -- last 500 chars of stdout/stderr
);

CREATE INDEX IF NOT EXISTS idx_cron_runs_job
    ON cron_runs(job_name, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_cron_runs_started
    ON cron_runs(started_at DESC);

CREATE INDEX IF NOT EXISTS idx_cron_runs_status
    ON cron_runs(status, started_at DESC);
