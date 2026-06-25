-- Migration 002: Memory embeddings cache table
-- Created by _migrate_memory_embeddings in the old system.

CREATE TABLE IF NOT EXISTS memory_embeddings (
    memory_id       TEXT PRIMARY KEY,
    content_hash    TEXT NOT NULL,
    embedding       BLOB NOT NULL,
    model_revision  TEXT NOT NULL,
    dim             INTEGER NOT NULL,
    updated_at      REAL NOT NULL,
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_memory_embeddings_hash ON memory_embeddings(content_hash);
CREATE INDEX IF NOT EXISTS idx_memory_embeddings_revision ON memory_embeddings(model_revision);
