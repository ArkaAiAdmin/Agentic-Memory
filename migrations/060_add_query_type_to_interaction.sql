-- 060: Search-pipeline SOTA — Add query_type to memory_search_interaction.
ALTER TABLE memory_search_interaction ADD COLUMN query_type TEXT;
CREATE INDEX IF NOT EXISTS idx_msi_query_type ON memory_search_interaction(query_type);
