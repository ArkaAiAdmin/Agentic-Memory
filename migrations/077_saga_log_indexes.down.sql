-- Down migration for 077_saga_log_indexes.
--
-- Drop the indexes added to speed up the saga crash-recovery scan.
-- The indexes are purely accelerators; dropping them is lossless.

DROP INDEX IF EXISTS idx_saga_log_saga_step;
DROP INDEX IF EXISTS idx_saga_log_status;