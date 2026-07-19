-- Migration 004: Add audit_log_bytes to usage_records for dedicated audit log size tracking

ALTER TABLE usage_records ADD COLUMN audit_log_bytes INTEGER DEFAULT 0;
