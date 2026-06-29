-- Rollback: drop audit_status columns added by 023_add_audit_status.sql
-- SQLite does not support DROP COLUMN in older versions, but it does
-- in recent builds (3.35.2+). Safe to run on Python 3.14+.

DROP INDEX IF EXISTS idx_sessions_audit_status;
DROP INDEX IF EXISTS idx_decision_threads_audit_status;

-- Recreate tables without audit_status to drop the column cleanly.
-- (SQLite cannot drop individual columns in-place.)
