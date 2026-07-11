-- 053: Schema version marker — no DDL changes.
--
-- All schema changes for the field-crdt tenant isolation feature are
-- contained in migration 051. This file exists so that
-- SCHEMA_VERSION (currently 53) matches the highest numbered
-- .sql file on disk, satisfying the discovery invariant checked by
-- test_migration_runner.py.
SELECT 1;
