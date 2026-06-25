-- Migration 007 down: drop the memory_skills table and its indexes.
--
-- Note: this drops ALL extracted skills. The extraction is idempotent —
-- re-running cron_skill_extraction.py after re-applying the up migration
-- will rebuild the cache from existing memories.

DROP INDEX IF EXISTS idx_memory_skills_topic;
DROP INDEX IF EXISTS idx_memory_skills_hit;
DROP TABLE IF EXISTS memory_skills;
