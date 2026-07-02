-- Down migration for 024: restore pre-024 schema

DROP TABLE IF EXISTS memory_chunk_vec_keys;
DROP TABLE IF EXISTS memory_chunk_vec_idx;
DROP TABLE IF EXISTS memory_chunk_embeddings;
DROP INDEX IF EXISTS idx_chunk_vec_keys_chunk;
DROP INDEX IF EXISTS idx_chunk_vec_keys_parent;
DROP INDEX IF EXISTS idx_chunk_embeddings_hash;
DROP INDEX IF EXISTS idx_chunk_embeddings_parent;
