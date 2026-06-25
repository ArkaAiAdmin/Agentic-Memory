-- Migration 001: Schema version table
-- This is the baseline migration. For backward compatibility with
-- DBs that have schema_version <= 4, this is treated as already applied.

CREATE TABLE IF NOT EXISTS schema_version (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
