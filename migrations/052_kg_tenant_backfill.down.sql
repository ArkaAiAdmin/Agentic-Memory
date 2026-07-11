-- 052 down: revert backfilled tenant_id values to the default.
-- The tenant_id column itself is owned by migration 050 and is not dropped here.

UPDATE kg_facts SET tenant_id = 'default' WHERE tenant_id <> 'default';
UPDATE kg_entities SET tenant_id = 'default' WHERE tenant_id <> 'default';
