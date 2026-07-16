-- 066_kg_crdt_tenant_id.down.sql
-- Reverse migration 066: drop the applied / fingerprint columns added to
-- the KG CRDT op-log tables.  tenant_id is left in place (owned by
-- migration 055).  fingerprint is only dropped from kg_entity_crdt
-- (edges never got it; see migration 066 header).

ALTER TABLE kg_entity_crdt DROP COLUMN applied;
ALTER TABLE kg_edge_crdt DROP COLUMN applied;
ALTER TABLE kg_entity_crdt DROP COLUMN fingerprint;
