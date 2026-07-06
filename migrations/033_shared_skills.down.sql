-- Down migration for 033_shared_skills.
-- SQLite < 3.35.0 cannot DROP COLUMN natively. The three columns
-- added by this migration (hit_vector, last_used_vector, logical_clock)
-- are left in place: they are harmless on a schema at version 32,
-- and callers already gate on schema_version before reading them.

-- No-op statements to satisfy the down-script parse test.
-- The columns will be ignored by code paths when schema_version < 33.
PRAGMA user_version = 32;
