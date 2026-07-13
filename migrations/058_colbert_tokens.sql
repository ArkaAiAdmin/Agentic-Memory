-- 058: ColBERT late-interaction token storage.
--
-- Stores per-token embeddings from ColBERT-v2 for MaxSim reranking.
-- Each memory chunk produces one row per token with its 128-dim vector.
--
-- Disk estimate: 128 * 4 bytes * ~120 tokens * 30k chunks ≈ 1.8 GB at scale.
-- Local-first tradeoff is acceptable for the accuracy gain.

CREATE TABLE IF NOT EXISTS colbert_tokens (
    id          INTEGER PRIMARY KEY,
    memory_id   TEXT NOT NULL,
    chunk_id    INTEGER NOT NULL DEFAULT 0,
    position    INTEGER NOT NULL DEFAULT 0,
    token_text  TEXT NOT NULL DEFAULT '',
    vec         BLOB NOT NULL,
    created_at  REAL NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_ct_memory ON colbert_tokens(memory_id);
CREATE INDEX IF NOT EXISTS idx_ct_chunk  ON colbert_tokens(memory_id, chunk_id);
