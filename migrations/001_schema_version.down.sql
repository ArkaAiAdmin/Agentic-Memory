-- Down migration 001: Drop schema_version table
-- Only safe if this is the last migration applied.
DROP TABLE IF EXISTS schema_version;
