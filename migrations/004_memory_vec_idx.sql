-- Migration 004: Vector index tables (usearch HNSW)
-- Singleton index blob + key -> memory_id mapping.

CREATE TABLE IF NOT EXISTS memory_vec_idx (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    n_vectors         INTEGER NOT NULL,
    dim               INTEGER NOT NULL,
    metric            TEXT    NOT NULL,
    quantization      TEXT    NOT NULL,
    connectivity      INTEGER NOT NULL,
    expansion_add     INTEGER NOT NULL,
    expansion_search  INTEGER NOT NULL,
    built_at          REAL    NOT NULL,
    index_blob        BLOB    NOT NULL,
    key_count         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_vec_keys (
    key         INTEGER PRIMARY KEY,
    memory_id   TEXT    NOT NULL UNIQUE REFERENCES memories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_vec_keys_memory_id ON memory_vec_keys(memory_id);
