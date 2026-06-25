-- Migration 016 down: drop concept_drift table and its index.
-- Reverses migrations/016_concept_drift.sql.

DROP INDEX IF EXISTS idx_drift_triggered;
DROP TABLE IF EXISTS concept_drift;
