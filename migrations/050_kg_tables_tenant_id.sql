-- 050: Add tenant_id to KG tables for cross-tenant data protection.
--
-- kg_entities and kg_facts were created before the tenant isolation
-- system was added (migration 000). Without a native tenant_id column,
-- cross-tenant KG data protection relies on correct walk-through logic
-- rather than column-level filtering.
--
-- This migration adds tenant_id to both tables with a DEFAULT of
-- 'default' for backward compatibility with existing single-tenant
-- deployments. Migration 042 already established the NOT NULL DEFAULT
-- 'default' pattern on the memories table.

ALTER TABLE kg_entities ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE kg_facts ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_kg_entities_tenant ON kg_entities(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kg_facts_tenant ON kg_facts(tenant_id);
