-- Migration 076: Add columns that were previously only added by Python
-- inline ALTER TABLE statements (Hard Rule 4 violation).
--
-- These columns existed solely through Python-level ALTER TABLE calls:
--   * knowledge_graph/kg_schema.py:108  (kg_entities.centrality)
--   * infra/db_migrations.py:288        (kg_edges.tenant_id)
--
-- Migration 050 already covers kg_entities.tenant_id and kg_facts.tenant_id.
-- Migration 000 already includes shared_memories.tenant_id in the base schema.
-- But kg_edges.tenant_id and kg_entities.centrality were never migrated.
--
-- Creating this numbered migration makes these schema changes visible,
-- reversible, and consistent with the rule that all schema changes
-- go through numbered migrations only.

-- kg_entities.centrality: graph centrality score for ranking
ALTER TABLE kg_entities ADD COLUMN centrality REAL DEFAULT 0.0;
CREATE INDEX IF NOT EXISTS idx_kg_entities_centrality ON kg_entities(centrality);

-- kg_edges.tenant_id: multi-tenant isolation for edge-level filtering
-- (kg_entities.tenant_id was added by migration 050; kg_edges was missed)
ALTER TABLE kg_edges ADD COLUMN tenant_id TEXT DEFAULT 'default';
CREATE INDEX IF NOT EXISTS idx_kg_edges_tenant_id ON kg_edges(tenant_id);
