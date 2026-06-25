-- Down migration 004: Drop vector index tables and their indexes
DROP INDEX IF EXISTS idx_vec_keys_memory_id;
DROP TABLE IF EXISTS memory_vec_keys;
DROP TABLE IF EXISTS memory_vec_idx;
