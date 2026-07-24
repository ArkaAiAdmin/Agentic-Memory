-- Migration 074 DOWN: drop the recreated indexes
DROP INDEX IF EXISTS idx_backlinks_source_id;
DROP INDEX IF EXISTS idx_memories_active;
