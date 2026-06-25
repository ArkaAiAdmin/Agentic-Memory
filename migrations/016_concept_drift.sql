-- Migration 016: concept_drift — make the table canonical SQL
--
-- 2026-06-22 (D1 fix): until now, the `concept_drift` table was
-- created in Python via `db_migrations._migrate_concept_drift`. That
-- violated AGENTS.md hard rule 7 ("Schema migrations go in
-- `migrations/NNN_name.sql` + `NNN_name.down.sql`. Add to
-- `migration_runner.MIGRATIONS` list.") — every other table has a
-- numbered .sql file, concept_drift was the lone holdout.
--
-- This migration moves the schema to the canonical location. The
-- Python helper stays as a safety net (CREATE TABLE IF NOT EXISTS) so
-- the system still works against un-migrated DBs that opened before
-- this commit.
--
-- The schema matches what `cron/cron_concept_drift.py:201` writes:
--   (id, drift_metric, drifted_dimensions, triggered_at)
-- `acknowledged` is the operator-acknowledgement flag (the cron
-- currently writes rows with acknowledged=0 by default; UPDATEs to 1
-- happen via `memory_check_concept_drift`).
--
-- Idempotent: CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS concept_drift (
    id                   TEXT PRIMARY KEY,
    drift_metric         REAL NOT NULL,
    drifted_dimensions   TEXT,
    triggered_at         REAL NOT NULL,
    acknowledged         INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_drift_triggered ON concept_drift(triggered_at);
