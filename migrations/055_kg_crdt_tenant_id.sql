-- 055: Add tenant_id to KG CRDT tables for tenant isolation.
--
-- kg_entity_crdt and kg_edge_crdt track per-peer CRDT operations
-- for knowledge graph sync. Without a native tenant_id column,
-- cross-tenant KG sync protection relies on correct walk-through
-- logic rather than column-level filtering.
--
-- This migration adds tenant_id to both tables with a DEFAULT of
-- 'default' for backward compatibility with existing single-tenant
-- deployments. Follows the pattern established in migration 050.

ALTER TABLE kg_entity_crdt ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE kg_edge_crdt ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_kg_entity_crdt_tenant ON kg_entity_crdt(tenant_id);
CREATE INDEX IF NOT EXISTS idx_kg_edge_crdt_tenant ON kg_edge_crdt(tenant_id);
