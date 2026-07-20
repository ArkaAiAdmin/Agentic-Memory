CREATE TABLE IF NOT EXISTS system_locks (
    lock_key TEXT PRIMARY KEY,
    holder_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    lease_token TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_locks_expires ON system_locks(expires_at);
