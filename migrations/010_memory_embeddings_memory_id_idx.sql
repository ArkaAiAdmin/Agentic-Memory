-- Migration 010: P1-17 fix — add an index on memory_embeddings.memory_id.
--
-- The schema for memory_embeddings has no index on memory_id. Queries
-- like ``DELETE FROM memory_embeddings WHERE memory_id = ?`` and
-- ``SELECT ... FROM memory_embeddings WHERE memory_id = ?`` do a
-- full table scan, which becomes a bottleneck as the embedding cache
-- grows past ~10K rows. The FK already references memories(id) with
-- ON DELETE CASCADE, but FK enforcement doesn't create a covering
-- index — it only creates a row-id reference for cascade. So an
-- explicit index is needed for the lookup paths the FK cascade does
-- not cover.
--
-- All statements are idempotent. The index IF NOT EXISTS is safe to
-- re-run; concurrent index builds are not relevant since this is a
-- migration, not a hot-path DDL.

CREATE INDEX IF NOT EXISTS idx_memory_embeddings_memory_id
    ON memory_embeddings(memory_id);
