-- Migration 003: Memory audit log table
-- Append-only log of MCP tool invocations for observability.

CREATE TABLE IF NOT EXISTS memory_audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    tool            TEXT    NOT NULL,
    args            TEXT,
    results_count   INTEGER,
    top1_id         TEXT,
    latency_ms      REAL    NOT NULL,
    error           TEXT,
    request_id      TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_log_tool_ts ON memory_audit_log(tool, ts);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON memory_audit_log(ts);
