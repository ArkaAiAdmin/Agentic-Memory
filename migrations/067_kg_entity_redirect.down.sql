-- 067_kg_entity_redirect.down.sql
-- Drop the durable entity redirect map added in 067.

DROP INDEX IF EXISTS idx_kg_entity_redirect_tenant;
DROP INDEX IF EXISTS idx_kg_entity_redirect_winner;
DROP TABLE IF EXISTS kg_entity_redirect;
