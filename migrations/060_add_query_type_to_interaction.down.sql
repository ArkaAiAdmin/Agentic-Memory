-- Rollback 060: Drop query_type column from memory_search_interaction.
DROP INDEX IF EXISTS idx_msi_query_type;
ALTER TABLE memory_search_interaction DROP COLUMN query_type;
