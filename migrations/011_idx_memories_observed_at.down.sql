-- 011_idx_memories_observed_at.down.sql
-- Remove the partial index added by 011_idx_memories_observed_at.sql

DROP INDEX IF EXISTS idx_memories_observed_at;
