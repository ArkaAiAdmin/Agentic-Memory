ALTER TABLE kg_entities ADD COLUMN community_id INTEGER DEFAULT 0;
ALTER TABLE kg_entities ADD COLUMN betweenness REAL DEFAULT 0.0;
CREATE INDEX IF NOT EXISTS idx_kg_entities_community_id
    ON kg_entities(community_id);
CREATE INDEX IF NOT EXISTS idx_kg_entities_betweenness
    ON kg_entities(betweenness);
