-- Down migration 010: drop the idx_memory_embeddings_memory_id index.
-- This is a destructive rollback path; the down migration is only
-- used to revert the schema to schema_version=9 in an emergency.

DROP INDEX IF EXISTS idx_memory_embeddings_memory_id;
