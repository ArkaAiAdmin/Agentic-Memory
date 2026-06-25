-- Migration 018: fact-level temporal validity for the knowledge graph
--
-- 2026-06-23 follow-up (T1 of the temporal-kg plan).  Adds the columns
-- needed to represent "a fact was true in the world between event_time
-- and invalid_at" — the bi-temporal model that powers time-travel
-- queries ("what did we know on date X?") and automatic supersession
-- (when a new fact contradicts an old one).
--
-- The schema mirrors the already-present note-level temporal columns on
-- the ``memories`` table (valid_from, valid_to, superseded_by).  The
-- note-level machinery in temporal_resolver.py + crdt_merge.py keeps
-- working unchanged; this migration adds the missing fact-level layer
-- that the kg_facts table needs.
--
-- New columns:
--   * event_time REAL                   -- when the fact was true in the world
--                                          (extracted from text: "as of March 2026",
--                                          "until 2024", etc.)
--   * event_time_granularity TEXT        -- precision: 'day' | 'month' | 'year' | 'unknown'
--   * transaction_time REAL              -- when WE learned it (default: now)
--   * valid_at REAL                      -- when the fact became true (event-time);
--                                          NULL = unknown (treat as 'always true')
--   * invalid_at REAL                    -- when it stopped being true; NULL = still valid
--   * superseded_by INTEGER               -- FK to kg_facts.id (the newer version)
--   * supersedes INTEGER                 -- FK to kg_facts.id (the older version)
--   * contradiction_score REAL           -- 0.0-1.0; how strongly the supersede was a contradiction
--   * invalidation_reason TEXT            -- 'superseded' | 'contradicted' | 'expired' | 'manual'
--
-- All new columns are NULL-able with NULL defaults so existing rows are
-- unaffected.  Backfill is trivial: no data movement needed.
--
-- New indexes (T1.5):
--   * idx_kg_facts_validity  -- (valid_at, invalid_at) for at-time queries
--   * idx_kg_facts_superseded_by            -- chain traversal
--   * idx_kg_facts_event_time               -- event-time ordering
--
-- Note: SQLite ALTER TABLE ADD COLUMN is idempotent only at the Python
-- level (the parser tracks "column already exists" via the schema
-- introspection).  The migration runner sees the column is present
-- in the table_info and skips re-adding.  The CREATE INDEX statements
-- use IF NOT EXISTS so they're naturally idempotent.

-- ---------------------------------------------------------------------------
-- 1. Add the new columns
-- ---------------------------------------------------------------------------

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

-- ---------------------------------------------------------------------------
-- 2. Backfill transaction_time for existing facts (we don't have
--    event_time, so leave it NULL = 'unknown')
-- ---------------------------------------------------------------------------

UPDATE kg_facts
SET transaction_time = COALESCE(first_seen, last_seen, 0.0)
WHERE transaction_time IS NULL
  AND (first_seen IS NOT NULL OR last_seen IS NOT NULL);

-- ---------------------------------------------------------------------------
-- 3. New indexes
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_kg_facts_validity
    ON kg_facts(valid_at, invalid_at);
CREATE INDEX IF NOT EXISTS idx_kg_facts_superseded_by
    ON kg_facts(superseded_by);
CREATE INDEX IF NOT EXISTS idx_kg_facts_event_time
    ON kg_facts(event_time);
