-- Down migration 065: Revert KG CRDT op log to state table

DROP TABLE IF EXISTS kg_entity_crdt_append;
DROP TABLE IF EXISTS kg_edge_crdt_append;
