-- Down migration 002: Drop memory_embeddings table and its indexes
DROP INDEX IF EXISTS idx_memory_embeddings_hash;
DROP INDEX IF EXISTS idx_memory_embeddings_revision;
DROP TABLE IF EXISTS memory_embeddings;
