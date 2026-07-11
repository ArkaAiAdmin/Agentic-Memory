-- 051: Add tenant_id column to memory_field_crdt for tenant isolation.
--
-- The memory_field_crdt table tracks per-field CRDT state for each memory.
-- This migration adds a tenant_id column (DEFAULT 'default') so that
-- field-level CRDT rows respect tenant boundaries. The column is populated
-- from the parent memory's tenant_id for existing rows.

ALTER TABLE memory_field_crdt ADD COLUMN tenant_id TEXT DEFAULT 'default';

-- Backfill tenant_id from parent memories for existing rows.
UPDATE memory_field_crdt
SET tenant_id = COALESCE((
    SELECT m.tenant_id FROM memories m WHERE m.id = memory_field_crdt.memory_id
), 'default')
WHERE COALESCE(tenant_id, 'default') = 'default';

CREATE INDEX IF NOT EXISTS idx_memory_field_crdt_tenant_id
    ON memory_field_crdt(tenant_id);
