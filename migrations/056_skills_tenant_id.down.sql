-- 056 down: Remove tenant_id from memory_skills.
-- SQLite does not support DROP COLUMN in older versions; drop the
-- index only (column remains but is unused on rollback).

DROP INDEX IF EXISTS idx_memory_skills_tenant;
