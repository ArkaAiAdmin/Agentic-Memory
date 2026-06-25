-- Down migration 003: Drop memory_audit_log table and its indexes
DROP INDEX IF EXISTS idx_audit_log_tool_ts;
DROP INDEX IF EXISTS idx_audit_log_ts;
DROP TABLE IF EXISTS memory_audit_log;
