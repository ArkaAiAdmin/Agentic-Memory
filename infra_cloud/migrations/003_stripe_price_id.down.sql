-- Down migration for 003: Remove Stripe Price ID from plans
-- SQLite doesn't support ALTER TABLE DROP COLUMN before 3.35.0.
-- Recreate the table without the column.
CREATE TABLE IF NOT EXISTS plans_backup (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    max_storage_mb INTEGER NOT NULL,
    max_mcp_calls_per_day INTEGER NOT NULL,
    max_seats INTEGER NOT NULL,
    retention_days INTEGER NOT NULL,
    features_json TEXT
);
INSERT INTO plans_backup SELECT id, name, max_storage_mb, max_mcp_calls_per_day, max_seats, retention_days, features_json FROM plans;
DROP TABLE plans;
ALTER TABLE plans_backup RENAME TO plans;
