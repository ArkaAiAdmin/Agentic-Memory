-- Migration 008: H24+ — sync_log table for auto multi-agent sync tracking.
--
-- Records every sync cycle (push, pull, or full sync) with duration,
-- change counts, and error aggregation so operators can monitor peer
-- health and diagnose failures without grepping text logs.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS sync_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_name        TEXT NOT NULL,
    peer_url         TEXT NOT NULL,
    peer_agent_id    TEXT NOT NULL,
    direction        TEXT NOT NULL CHECK (direction IN ('push', 'pull', 'sync')),
    started_at       REAL NOT NULL,
    completed_at     REAL,
    success          INTEGER DEFAULT 0,
    changes_pushed   INTEGER DEFAULT 0,
    changes_pulled   INTEGER DEFAULT 0,
    error_message    TEXT,
    error_count      INTEGER DEFAULT 0,
    duration_ms      INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sync_log_peer ON sync_log(peer_name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sync_log_success ON sync_log(success, started_at DESC);
