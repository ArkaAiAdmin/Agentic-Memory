-- Migration 066 down: remove the tenant_id / applied columns added by 066.
-- Data-trimming only: the op-log content is preserved.

ALTER TABLE kg_entity_crdt DROP COLUMN tenant_id;
ALTER TABLE kg_edge_crdt DROP COLUMN tenant_id;
ALTER TABLE kg_entity_crdt DROP COLUMN applied;
ALTER TABLE kg_edge_crdt DROP COLUMN applied;
ALTER TABLE kg_entity_crdt DROP COLUMN fingerprint;
