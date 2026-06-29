-- Add audit_status column to session memory tables (P0.2 fix).
-- Tracks whether the save_memory audit trail write succeeded.
-- "ok"    = both v22 write and audit trail succeeded (default)
-- "pending" = v22 write succeeded but save_memory failed or raised

ALTER TABLE sessions ADD COLUMN audit_status TEXT DEFAULT 'ok';
ALTER TABLE decision_threads ADD COLUMN audit_status TEXT DEFAULT 'ok';
ALTER TABLE thread_events ADD COLUMN audit_status TEXT DEFAULT 'ok';
ALTER TABLE session_compaction_log ADD COLUMN audit_status TEXT DEFAULT 'ok';

CREATE INDEX IF NOT EXISTS idx_sessions_audit_status ON sessions(audit_status);
CREATE INDEX IF NOT EXISTS idx_decision_threads_audit_status ON decision_threads(audit_status);
