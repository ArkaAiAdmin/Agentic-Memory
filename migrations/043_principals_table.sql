-- 043: Add principals and principal_identities tables for tenant identity management.

CREATE TABLE IF NOT EXISTS principals (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL DEFAULT 'user',
    display_name TEXT,
    tenant_id   TEXT NOT NULL DEFAULT 'default',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS principal_identities (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    principal_id  TEXT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    provider      TEXT NOT NULL,
    external_sub  TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(provider, external_sub)
);

CREATE INDEX IF NOT EXISTS idx_principals_tenant ON principals(tenant_id);
CREATE INDEX IF NOT EXISTS idx_principal_identities_principal ON principal_identities(principal_id);
