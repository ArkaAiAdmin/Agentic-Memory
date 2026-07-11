-- 052: Backfill tenant_id on kg_facts / kg_entities from their parent memory.
--
-- Migration 050 added the tenant_id column (default 'default'); this
-- populates it from memories.tenant_id via the kg_facts.source_memory
-- linkage so multi-tenant upgrades do not silently tag every fact as
-- tenant 'default' (GAP 2). Rows already holding a non-default tenant
-- (e.g. freshly written post-050) are left untouched.

UPDATE kg_facts
SET tenant_id = (
    SELECT m.tenant_id FROM memories m WHERE m.id = kg_facts.source_memory
)
WHERE source_memory IS NOT NULL
  AND COALESCE(tenant_id, 'default') = 'default';

UPDATE kg_entities
SET tenant_id = (
    SELECT m.tenant_id FROM memories m
    JOIN kg_facts kf ON kf.source_memory = m.id
    WHERE kf.subject_entity_id = kg_entities.id
       OR kf.object_entity_id = kg_entities.id
    LIMIT 1
)
WHERE COALESCE(tenant_id, 'default') = 'default';
