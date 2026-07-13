-- 059: SPLADE sparse vector storage for hybrid search.
--
-- Stores sparse vocabulary-weight pairs from SPLADE-v3 encoding.
-- Each memory produces a variable number of (vocab_id, weight) pairs
-- representing its learned sparse representation.
--
-- Disk estimate: ~100-300 pairs per memory × 6.9k memories × 8 bytes
-- = ~35 MB at scale.  Much smaller than dense embeddings.

CREATE TABLE IF NOT EXISTS splade_tokens (
    id          INTEGER PRIMARY KEY,
    memory_id   TEXT NOT NULL,
    vocab_id    INTEGER NOT NULL,
    weight      REAL NOT NULL,
    created_at  REAL NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_st_memory ON splade_tokens(memory_id);
CREATE INDEX IF NOT EXISTS idx_st_vocab  ON splade_tokens(vocab_id);
