-- 015_drift_alarms.down.sql
-- v15 rollback: drop the drift_alarms table and its indexes.
--
-- Only used to revert to schema_version=13 in an emergency.
-- The pre-existing `concept_drift` table is NOT touched here —
-- it's a separate schema entity and is preserved by the rollback.

DROP INDEX IF EXISTS idx_drift_alarms_unack;
DROP INDEX IF EXISTS idx_drift_alarms_detected;
DROP INDEX IF EXISTS idx_drift_alarms_memory;
DROP TABLE IF EXISTS drift_alarms;
