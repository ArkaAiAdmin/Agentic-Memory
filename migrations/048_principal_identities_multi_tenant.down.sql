-- 048 down: revert to single-tenant UNIQUE(provider, external_sub).
-- Drops tenant_id column and the per-tenant uniqueness to restore the
-- original constraint.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS principal_identities_pre048 (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    principal_id  TEXT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    provider      TEXT NOT NULL,
    external_sub  TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(provider, external_sub)
);

INSERT INTO principal_identities_pre048 (id, principal_id, provider, external_sub, created_at)
SELECT id, principal_id, provider, external_sub, created_at
FROM principal_identities;

DROP TABLE principal_identities;

ALTER TABLE principal_identities_pre048 RENAME TO principal_identities;

CREATE INDEX IF NOT EXISTS idx_principal_identities_principal
    ON principal_identities(principal_id);

PRAGMA foreign_keys = ON;
