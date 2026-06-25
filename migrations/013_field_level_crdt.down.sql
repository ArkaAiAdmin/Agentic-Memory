-- 013_field_level_crdt.down.sql
-- Drop the per-field CRDT table. Destructive rollback; only used to
-- revert to schema_version=12 in an emergency. The note-level
-- `memories.version_vector` and `memories.logical_clock` columns
-- are NOT dropped here (they predate v13 and are read by the
-- legacy note-level LWW code path in crdt_merge.py).

DROP INDEX IF EXISTS idx_memory_field_crdt_agent_updated;
DROP INDEX IF EXISTS idx_memory_field_crdt_memory;
DROP TABLE IF EXISTS memory_field_crdt;
