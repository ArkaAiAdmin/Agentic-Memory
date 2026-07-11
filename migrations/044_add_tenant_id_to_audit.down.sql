-- 044 down: Remove tenant_id and principal_id from memory_audit_log.
--
-- SQLite doesn't support DROP COLUMN, so recreate the table.

CREATE TABLE memory_audit_log_old (
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

INSERT INTO memory_audit_log_old (
    id, ts, tool, args, results_count, top1_id, latency_ms, error, request_id
)
SELECT
    id, ts, tool, args, results_count, top1_id, latency_ms, error, request_id
FROM memory_audit_log;

DROP TABLE memory_audit_log;
ALTER TABLE memory_audit_log_old RENAME TO memory_audit_log;

CREATE INDEX idx_audit_log_tool_ts ON memory_audit_log(tool, ts);
CREATE INDEX idx_audit_log_ts ON memory_audit_log(ts);
