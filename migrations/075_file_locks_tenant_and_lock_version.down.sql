-- Down migration 075: Remove lock_version and tenant_id from file_locks.

DROP INDEX IF EXISTS idx_file_locks_tenant;
ALTER TABLE file_locks DROP COLUMN tenant_id;
ALTER TABLE file_locks DROP COLUMN lock_version;
