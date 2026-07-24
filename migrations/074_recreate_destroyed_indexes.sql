-- Migration 074: recreate indexes destroyed by table-recreation migrations
--
-- Two indexes were silently destroyed when later migrations recreated
-- tables using the SQLite 12-step ALTER TABLE pattern:
--
-- 1. idx_backlinks_source_id — created by migration 006, destroyed by
--    migration 017 (which recreates backlinks with ON DELETE CASCADE but
--    only recreates idx_backlinks_target_id, forgetting the source_id index).
--
-- 2. idx_memories_active — created by migration 006 as a partial index
--    (WHERE valid_to IS NULL OR valid_to = ''), destroyed by migration 042
--    (which recreates memories with tenant_id NOT NULL but omits this
--    partial index from its 13 index recreations).
--
-- This migration is purely additive: CREATE INDEX IF NOT EXISTS is safe
-- on existing indexes and fixes the missing indexes on fresh installs.

-- Recreate idx_backlinks_source_id
CREATE INDEX IF NOT EXISTS idx_backlinks_source_id ON backlinks(source_id);

-- Recreate idx_memories_active (partial index for temporal validity filter)
-- search_pipeline.py:2409 does SELECT id FROM memories WHERE valid_to IS NULL
-- on every search.  Without this partial index it's O(N) instead of O(log N).
CREATE INDEX IF NOT EXISTS idx_memories_active
    ON memories(id) WHERE valid_to IS NULL OR valid_to = '';
