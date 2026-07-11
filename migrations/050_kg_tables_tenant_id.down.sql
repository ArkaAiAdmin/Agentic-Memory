-- 050 down: Remove tenant_id from KG tables.
-- SQLite doesn't support DROP COLUMN for earlier versions; this is a
-- no-op migration (the column is harmless if left in place). For a full
-- revert, restore from a backup taken before migration 050.

-- Note: SQLite 3.42+ supports DROP COLUMN. These statements are
-- commented out so they can be un-commented by an operator running
-- SQLite 3.42+ if a revert is required.

-- ALTER TABLE kg_entities DROP COLUMN tenant_id;
-- ALTER TABLE kg_facts DROP COLUMN tenant_id;
-- DROP INDEX IF EXISTS idx_kg_entities_tenant;
-- DROP INDEX IF EXISTS idx_kg_facts_tenant;

-- For SQLite < 3.42, recreate the tables without the column:
-- PRAGMA foreign_keys=OFF;
-- CREATE TABLE kg_entities_v2 (...);  -- copy schema from 000 without tenant_id
-- INSERT INTO kg_entities_v2 SELECT id,name,entity_type,mentions,created_at,updated_at FROM kg_entities;
-- DROP TABLE kg_entities;
-- ALTER TABLE kg_entities_v2 RENAME TO kg_entities;
-- (repeat for kg_facts, kg_edges with caution for FKs)
-- PRAGMA foreign_keys=ON;
