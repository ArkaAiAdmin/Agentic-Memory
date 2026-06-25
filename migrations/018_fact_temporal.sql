-- Migration 018: fact-level temporal validity for the knowledge graph
--
-- 2026-06-23 follow-up (T1 of the temporal-kg plan).  Adds the columns
-- needed to represent "a fact was true in the world between event_time
-- and invalid_at" — the bi-temporal model that powers time-travel
-- queries ("what did we know on date X?") and automatic supersession
-- (when a new fact contradicts an old one).
--
-- --- Upgrade-only best-effort path ---
--
-- This migration is a no-op on fresh databases.  Migration 019 creates
-- the kg_facts table with all of these columns already present in the
-- table definition, so on a fresh install 019 produces a complete schema
-- and none of the ALTER TABLE statements below need to run.
--
-- This migration only adds value when upgrading a database that already
-- has kg_facts (created by an earlier path) but lacks the temporal
-- columns.  In that case the ALTER TABLEs succeed and the backfill runs.
--
-- If kg_facts does not exist yet, the migration runner catches the
-- "no such table" errors and continues — this is intentional.  Do NOT
-- add a CREATE TABLE here; 019 owns the authoritative table definition.
--
-- Reference schema (matching 019's CREATE TABLE):
--   event_time, event_time_granularity, transaction_time,
--   valid_at, invalid_at, superseded_by, supersedes,
--   contradiction_score, invalidation_reason

-- Only attempt column adds if kg_facts exists and the column is missing.
-- SQLite has no ALTER TABLE IF NOT COLUMN EXISTS, so we rely on the
-- migration runner's per-statement exception handling to swallow the
-- "no such table" error on fresh DBs.

ALTER TABLE kg_facts ADD COLUMN event_time REAL;
ALTER TABLE kg_facts ADD COLUMN event_time_granularity TEXT;
ALTER TABLE kg_facts ADD COLUMN transaction_time REAL;
ALTER TABLE kg_facts ADD COLUMN valid_at REAL;
ALTER TABLE kg_facts ADD COLUMN invalid_at REAL;
ALTER TABLE kg_facts ADD COLUMN superseded_by INTEGER
    REFERENCES kg_facts(id) ON DELETE SET NULL;
ALTER TABLE kg_facts ADD COLUMN supersedes INTEGER
    REFERENCES kg_facts(id) ON DELETE SET NULL;
ALTER TABLE kg_facts ADD COLUMN contradiction_score REAL DEFAULT 0.0;
ALTER TABLE kg_facts ADD COLUMN invalidation_reason TEXT;

-- Backfill transaction_time for existing facts (only runs if table exists).
UPDATE kg_facts
SET transaction_time = COALESCE(first_seen, last_seen, 0.0)
WHERE transaction_time IS NULL
  AND (first_seen IS NOT NULL OR last_seen IS NOT NULL);

-- Indexes — IF NOT EXISTS makes these naturally idempotent.
CREATE INDEX IF NOT EXISTS idx_kg_facts_validity
    ON kg_facts(valid_at, invalid_at);
CREATE INDEX IF NOT EXISTS idx_kg_facts_superseded_by
    ON kg_facts(superseded_by);
CREATE INDEX IF NOT EXISTS idx_kg_facts_event_time
    ON kg_facts(event_time);
