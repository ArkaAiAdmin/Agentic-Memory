-- 068_saga_audit_log.sql
-- Durable audit record for saga rollback failures.
--
-- When a saga step fails and the compensating undos raise, the
-- SagaError.rollback_errors list is in-memory only — it disappears when
-- the exception is caught and discarded.  This table persists a minimal
-- record so operators can query "what rollbacks failed recently?" for
-- post-mortem analysis.
--
-- Additive only.

CREATE TABLE IF NOT EXISTS saga_audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    saga_name       TEXT    NOT NULL,
    failed_step     TEXT    NOT NULL,
    original_error  TEXT,
    rollback_count  INTEGER NOT NULL DEFAULT 0,
    rollback_errors TEXT
);

CREATE INDEX IF NOT EXISTS idx_saga_audit_ts
    ON saga_audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_saga_audit_saga_name
    ON saga_audit_log(saga_name);
