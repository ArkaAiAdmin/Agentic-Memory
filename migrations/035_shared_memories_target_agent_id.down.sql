-- Down migration for 035_shared_memories_target_agent_id.
-- SQLite cannot DROP COLUMN before 3.35.0; the added columns
-- (target_agent_id, shared_with) and their indexes are harmless
-- at lower schema versions since callers gate on schema_version.
-- Set schema_version back to 34 to complete the rollback.
PRAGMA user_version = 34;
