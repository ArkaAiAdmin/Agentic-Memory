-- Down migration for 076_add_missing_columns.
--
-- SQLite has limited ALTER TABLE DROP COLUMN support (3.35+).
-- Rather than recreating the tables to remove columns, we drop the
-- indexes we created. The columns are harmless if left in place
-- defaults keep working for code that reads them.
--
-- This is the standard pattern for additive-only down migrations
-- (see 030_community_id_and_betweenness.down.sql for precedent).

DROP INDEX IF EXISTS idx_kg_entities_centrality;
DROP INDEX IF EXISTS idx_kg_edges_tenant_id;
