-- 062 down: Remove data_subject_sub column from memories.
-- SQLite < 3.35 has no DROP COLUMN; drop the index and leave the
-- nullable column in place (harmless — unused after rollback).
DROP INDEX IF EXISTS idx_memories_data_subject_sub;
