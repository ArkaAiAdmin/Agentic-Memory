-- Migration 020 down: drop kg_facts FTS5 index + sync triggers
--
-- Reverses migrations/020_kg_facts_fts.sql.
-- Drops the kg_facts_fts FTS5 virtual table and its 3 sync triggers.
--
-- Idempotent: uses IF EXISTS guards.

DROP TRIGGER IF EXISTS kg_facts_fts_ai;
DROP TRIGGER IF EXISTS kg_facts_fts_ad;
DROP TRIGGER IF EXISTS kg_facts_fts_au;
DROP TABLE IF EXISTS kg_facts_fts;
