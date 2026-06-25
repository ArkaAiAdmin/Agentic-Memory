-- 012_kg_extraction_stats.down.sql
-- Drop the kg_extraction_stats observability table. This is a
-- destructive rollback path; only used to revert to schema_version=11
-- in an emergency. The table is purely observability — no other
-- code depends on it for correctness.

DROP INDEX IF EXISTS idx_kg_extraction_stats_memory;
DROP INDEX IF EXISTS idx_kg_extraction_stats_created;
DROP TABLE IF EXISTS kg_extraction_stats;
