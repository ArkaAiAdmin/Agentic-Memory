-- B3.1: Add target_agent_id and shared_with columns to shared_memories
-- to support directed sharing and the shared_with_me filter.
-- NOTE: On a fresh DB, migration 000 already creates shared_memories
-- with these columns.  The ALTER TABLE statements produce "duplicate
-- column name" warnings that are caught as idempotent skips by the
-- runner.  On older DBs (pre-000) they add the missing columns.
ALTER TABLE shared_memories ADD COLUMN target_agent_id TEXT DEFAULT NULL;
ALTER TABLE shared_memories ADD COLUMN shared_with TEXT DEFAULT NULL;
CREATE INDEX IF NOT EXISTS idx_shared_target_agent ON shared_memories(target_agent_id);
CREATE INDEX IF NOT EXISTS idx_shared_shared_with ON shared_memories(shared_with);
