-- Migration 066: Align KG CRDT op-log tables with the append-only design
-- Sprint 3 / multi-tenant isolation:
--   * The live kg_entity_crdt / kg_edge_crdt tables (created by migration
--     021) lack the `tenant_id` column the sync server filters/writes by,
--     causing "no such column: tenant_id" on every KG push/pull.
--   * `ensure_kg_crdt_schema` expects an `applied` column (track projection
--     state) that older DBs also lack.
-- This migration adds both columns idempotently. SCHEMA_VERSION stays 64
-- (additive only).
--
-- The IF-NOT-EXISTS-style guard is done in Python by the migration runner's
-- idempotent re-apply; here we keep the plain ALTER and rely on the runner's
-- "object already exists" tolerance for re-runs.

ALTER TABLE kg_entity_crdt ADD COLUMN tenant_id TEXT DEFAULT 'default';
ALTER TABLE kg_edge_crdt ADD COLUMN tenant_id TEXT DEFAULT 'default';
ALTER TABLE kg_entity_crdt ADD COLUMN applied INTEGER DEFAULT 0;
ALTER TABLE kg_edge_crdt ADD COLUMN applied INTEGER DEFAULT 0;
ALTER TABLE kg_entity_crdt ADD COLUMN fingerprint TEXT;
