-- 055 down: Remove tenant_id from KG CRDT tables.
-- SQLite does not support DROP COLUMN in older versions; drop the
-- indexes only (columns remain but are unused on rollback).

DROP INDEX IF EXISTS idx_kg_entity_crdt_tenant;
DROP INDEX IF EXISTS idx_kg_edge_crdt_tenant;
