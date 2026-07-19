-- Migration 004 down: Remove audit_log_bytes from usage_records

ALTER TABLE usage_records DROP COLUMN audit_log_bytes;
