ALTER TABLE kg_facts ADD COLUMN is_entailed BOOLEAN DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_kg_facts_entailed ON kg_facts(is_entailed) WHERE is_entailed = 1;
