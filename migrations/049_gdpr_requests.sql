-- 049: GDPR Right-to-Be-Forgotten request tracking table.
-- Records deletion requests and their certificates for audit.

CREATE TABLE IF NOT EXISTS gdpr_requests (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    data_subject_hash TEXT NOT NULL,
    requested_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'completed', 'failed')),
    deletion_certificate_json TEXT,
    tenant_id TEXT NOT NULL DEFAULT 'default'
);

CREATE INDEX IF NOT EXISTS idx_gdpr_requests_subject_hash
    ON gdpr_requests(data_subject_hash);

CREATE INDEX IF NOT EXISTS idx_gdpr_requests_principal
    ON gdpr_requests(principal_id);

CREATE INDEX IF NOT EXISTS idx_gdpr_requests_tenant
    ON gdpr_requests(tenant_id);
