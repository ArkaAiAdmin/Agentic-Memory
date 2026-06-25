-- Down migration 005: Drop columns, indexes, and chunks table
-- Note: SQLite does not support DROP COLUMN. These columns are left in place.
-- The columns added are: valid_from, valid_to, superseded_by, last_accessed,
-- deleted_at, deleted_by, context_prefix, category, tier, importance_score, metadata

-- Drop indexes
DROP INDEX IF EXISTS idx_memories_repo_id;
DROP INDEX IF EXISTS idx_memories_pinned;
DROP INDEX IF EXISTS idx_memories_consolidation_state;
DROP INDEX IF EXISTS idx_memories_created_at;
DROP INDEX IF EXISTS idx_memories_updated_at;
DROP INDEX IF EXISTS idx_memories_observed_at;
DROP INDEX IF EXISTS idx_memories_fitness_score;
DROP INDEX IF EXISTS idx_memories_source_file;
DROP INDEX IF EXISTS idx_backlinks_target_id;
DROP INDEX IF EXISTS idx_memories_valid_to;
DROP INDEX IF EXISTS idx_memories_valid_from;
DROP INDEX IF EXISTS idx_memories_superseded_by;
DROP INDEX IF EXISTS idx_memories_last_accessed;
DROP INDEX IF EXISTS idx_memories_deleted_at;

-- Drop chunks table and its indexes
DROP INDEX IF EXISTS idx_memory_chunks_parent_id;
DROP TABLE IF EXISTS memory_chunks;
DROP TABLE IF EXISTS memory_chunks_fts;

-- Note: FTS5 triggers (memories_ai, memories_au, memories_ad) are NOT dropped
-- because they are needed by the FTS5 virtual table. They are created by
-- _migrate_ensure_fts_triggers in memory_common.py.
