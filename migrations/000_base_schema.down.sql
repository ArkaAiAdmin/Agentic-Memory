-- Down migration 000: Drop base schema tables
-- Must drop in reverse dependency order (child tables before parents).
-- kg_edges references kg_entities ON DELETE CASCADE, so drop kg_edges first.
-- kg_facts depends on kg_entities (via subject_entity_id/object_entity_id in
-- later migrations, but here only via source_memory -> memories).

DROP TABLE IF EXISTS kg_facts;
DROP TABLE IF EXISTS kg_edges;
DROP TABLE IF EXISTS kg_entities_fts;
DROP TABLE IF EXISTS kg_entities;
DROP TABLE IF EXISTS memories;
