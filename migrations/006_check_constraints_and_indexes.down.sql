-- Migration 006 DOWN: drop the CHECK constraint and new indexes.
-- Note: SQLite cannot drop a CHECK constraint from a table without
-- rebuilding. We rename-and-recreate, restoring the prior schema.
-- This is destructive; the down path is for emergency rollback only.

-- Drop new indexes
DROP INDEX IF EXISTS idx_shared_memories_shared_at;
DROP INDEX IF EXISTS idx_backlinks_source_id;
DROP INDEX IF EXISTS idx_memories_active;

-- Drop task_queue entirely so the up migration can recreate it with
-- the CHECK constraint.  SQLite cannot add CHECK via ALTER TABLE, so
-- the only way to restore the constraint on re-upgrade is to let the
-- up migration's CREATE TABLE IF NOT EXISTS run against a missing table.
DROP TABLE IF EXISTS task_queue;
