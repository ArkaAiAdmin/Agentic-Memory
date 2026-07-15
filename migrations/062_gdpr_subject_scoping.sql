-- 062: Add data_subject_sub column to memories for GDPR subject-scoped erase.
--
-- Previously gdpr_erase() accepted a data_subject_sub parameter but deleted
-- ALL memories in the tenant — a full tenant wipe rather than a subject-scoped
-- erase.  This migration adds the column so the erase path can filter by subject.
--
-- The column is nullable: existing rows get NULL (meaning "unscoped").  The
-- gdpr_erase function treats NULL as "skip subject filter" for backward
-- compatibility with pre-062 data.

ALTER TABLE memories ADD COLUMN data_subject_sub TEXT;
CREATE INDEX IF NOT EXISTS idx_memories_data_subject_sub ON memories(data_subject_sub);
