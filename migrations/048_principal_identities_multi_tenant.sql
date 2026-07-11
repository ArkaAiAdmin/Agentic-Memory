-- 048: Make principal_identities multi-tenant aware.
--
-- The previous UNIQUE(provider, external_sub) constraint prevented the
-- same identity (e.g. "okta / alice@example.com") from being linked to
-- different principals in different tenants. This blocked the multi-tenant
-- design where each tenant gets its own principal row for the same user.
--
-- Change: recreate the table with an added tenant_id column and
-- UNIQUE(provider, external_sub, tenant_id) so that the same identity
-- can exist independently per tenant.
--
-- No downstream table references principal_identities via FK, so the
-- DROP + RENAME is safe under PRAGMA foreign_keys = OFF for the
-- migration window.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS principal_identities_v048 (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    principal_id  TEXT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    provider      TEXT NOT NULL,
    external_sub  TEXT NOT NULL,
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(provider, external_sub, tenant_id)
);

INSERT INTO principal_identities_v048 (id, principal_id, provider, external_sub, tenant_id, created_at)
SELECT pi.id, pi.principal_id, pi.provider, pi.external_sub,
       COALESCE(p.tenant_id, 'default'), pi.created_at
FROM principal_identities pi
LEFT JOIN principals p ON p.id = pi.principal_id;

DROP TABLE principal_identities;

ALTER TABLE principal_identities_v048 RENAME TO principal_identities;

CREATE INDEX IF NOT EXISTS idx_principal_identities_principal
    ON principal_identities(principal_id);

PRAGMA foreign_keys = ON;
