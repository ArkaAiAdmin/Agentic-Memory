-- 044: Add tenant_id and principal_id to memory_audit_log.
--
-- SQLite doesn't support DROP COLUMN (pre-3.35), so we recreate the table.

-- Step 1: Create new table with additional columns
CREATE TABLE memory_audit_log_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    tool            TEXT    NOT NULL,
    args            TEXT,
    results_count   INTEGER,
    top1_id         TEXT,
    latency_ms      REAL    NOT NULL,
    error           TEXT,
    request_id      TEXT,
    tenant_id       TEXT    DEFAULT 'default',
    principal_id    TEXT
);

-- Step 2: Copy existing data
INSERT INTO memory_audit_log_new (
    id, ts, tool, args, results_count, top1_id, latency_ms, error, request_id
)
SELECT
    id, ts, tool, args, results_count, top1_id, latency_ms, error, request_id
FROM memory_audit_log;

-- Step 3: Drop old table and rename new
DROP TABLE memory_audit_log;
ALTER TABLE memory_audit_log_new RENAME TO memory_audit_log;

-- Step 4: Recreate indexes
CREATE INDEX idx_audit_log_tool_ts ON memory_audit_log(tool, ts);
CREATE INDEX idx_audit_log_ts ON memory_audit_log(ts);
CREATE INDEX idx_audit_tenant ON memory_audit_log(tenant_id);
