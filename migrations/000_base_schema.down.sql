-- Down migration 000: Drop base schema tables
-- Must drop in reverse dependency order (child tables before parents).
-- kg_edges references kg_entities ON DELETE CASCADE, so drop kg_edges first.
-- kg_facts depends on kg_entities (via subject_entity_id/object_entity_id in
-- later migrations, but here only via source_memory -> memories).

-- Tables added in the 2026-07-24 expansion of 000_base_schema.sql
DROP TABLE IF EXISTS saga_log;
DROP TABLE IF EXISTS review_schedule;
DROP TABLE IF EXISTS answer_rerank_cache;
DROP TABLE IF EXISTS dead_letter_messages;
DROP TABLE IF EXISTS user_access_log;
DROP TABLE IF EXISTS file_mtimes;
DROP TABLE IF EXISTS search_phase_stats;
DROP TABLE IF EXISTS user_profile_access_log;
DROP TABLE IF EXISTS shared_memories;
DROP TABLE IF EXISTS backlinks;

-- Original base tables
DROP TABLE IF EXISTS kg_facts;
DROP TABLE IF EXISTS kg_edges;
DROP TABLE IF EXISTS kg_entities_fts;
DROP TABLE IF EXISTS kg_entities;
DROP TABLE IF EXISTS memories;
