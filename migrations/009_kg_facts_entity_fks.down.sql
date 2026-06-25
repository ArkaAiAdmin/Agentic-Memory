-- Down: remove entity_id FK columns and indexes
DROP INDEX IF EXISTS idx_kg_facts_subject_entity;
DROP INDEX IF EXISTS idx_kg_facts_object_entity;
-- Note: SQLite doesn't support DROP COLUMN in older versions.  Recreate
-- the table without the new columns if you need to roll back fully.
