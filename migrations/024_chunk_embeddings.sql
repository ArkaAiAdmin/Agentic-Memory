-- Migration 024: Chunk-level embeddings + multi-vector ANN support
--
-- Adds:
--   memory_chunk_embeddings  — per-chunk FP32 vector cache
--   memory_chunk_vec_idx     — separate usearch HNSW index BLOB for chunks
--   memory_chunk_vec_keys    — stable key -> (chunk_id, parent_id) map
--
-- memory_vec_keys (memory-level) is not modified; chunks use their own index.

-- Per-chunk embedding cache ------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_chunk_embeddings (
    chunk_id       INTEGER PRIMARY KEY,  -- matches memory_chunks.id
    parent_id      TEXT    NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    content_hash   TEXT    NOT NULL,
    embedding      BLOB    NOT NULL,
    model_revision TEXT    NOT NULL,
    dim            INTEGER NOT NULL,
    updated_at     REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_parent
    ON memory_chunk_embeddings(parent_id);
CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_hash
    ON memory_chunk_embeddings(content_hash, model_revision);

-- Chunk-level ANN index singleton ------------------------------------------
CREATE TABLE IF NOT EXISTS memory_chunk_vec_idx (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    n_vectors        INTEGER NOT NULL,
    dim              INTEGER NOT NULL,
    metric           TEXT    NOT NULL,
    quantization     TEXT    NOT NULL,
    connectivity     INTEGER NOT NULL,
    expansion_add    INTEGER NOT NULL,
    expansion_search INTEGER NOT NULL,
    built_at         REAL    NOT NULL,
    index_blob       BLOB    NOT NULL,
    key_count        INTEGER NOT NULL
);

-- Chunk key -> chunk_id mapping -------------------------------------------
CREATE TABLE IF NOT EXISTS memory_chunk_vec_keys (
    key        INTEGER PRIMARY KEY,
    chunk_id   INTEGER NOT NULL REFERENCES memory_chunk_embeddings(chunk_id) ON DELETE CASCADE,
    parent_id  TEXT    NOT NULL REFERENCES memories(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chunk_vec_keys_parent
    ON memory_chunk_vec_keys(parent_id);
CREATE INDEX IF NOT EXISTS idx_chunk_vec_keys_chunk
    ON memory_chunk_vec_keys(chunk_id);
