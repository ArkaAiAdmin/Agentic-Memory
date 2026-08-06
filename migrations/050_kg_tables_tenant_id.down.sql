-- 050 down: Remove tenant_id from KG tables.
-- SQLite 3.42+ supports DROP COLUMN. Drop dependent indexes first,
-- then drop the columns — SQLite refuses to drop a column that is
-- still referenced by a live index.

DROP INDEX IF EXISTS idx_kg_entities_tenant;
DROP INDEX IF EXISTS idx_kg_facts_tenant;
DROP TRIGGER IF EXISTS kg_entities_fts_ai;
DROP TRIGGER IF EXISTS kg_entities_fts_ad;
DROP TRIGGER IF EXISTS kg_entities_fts_au;
DROP TABLE IF EXISTS kg_entities_fts;
ALTER TABLE kg_entities DROP COLUMN tenant_id;
ALTER TABLE kg_facts DROP COLUMN tenant_id;
