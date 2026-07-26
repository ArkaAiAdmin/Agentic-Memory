-- Migration 075: Add lock_version and tenant_id to file_locks
--
-- Migration 069 created the file_locks table without these columns.
-- This migration adds them for optimistic-lock support and tenant scoping.

ALTER TABLE file_locks ADD COLUMN lock_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE file_locks ADD COLUMN tenant_id TEXT DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_file_locks_tenant ON file_locks(tenant_id);
