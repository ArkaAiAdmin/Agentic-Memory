-- Migration 021: Down — Drop Graph CRDT tables
DROP INDEX IF EXISTS idx_kg_edge_crdt_pair;
DROP INDEX IF EXISTS idx_kg_edge_crdt_ts;
DROP INDEX IF EXISTS idx_kg_edge_crdt_agent;
DROP TABLE IF EXISTS kg_edge_crdt;
DROP INDEX IF EXISTS idx_kg_entity_crdt_ts;
DROP INDEX IF EXISTS idx_kg_entity_crdt_agent;
DROP TABLE IF EXISTS kg_entity_crdt;
