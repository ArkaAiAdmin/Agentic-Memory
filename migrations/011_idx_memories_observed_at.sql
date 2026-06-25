-- 011_idx_memories_observed_at.sql
-- Add a partial index on memories(observed_at DESC) filtered to live rows.
-- Powers _top_recent_notes and _top_recent_source_files without a full scan
-- and without surfacing soft-deleted rows.

CREATE INDEX IF NOT EXISTS idx_memories_observed_at
    ON memories(observed_at DESC)
    WHERE deleted_at IS NULL;
