-- Migration 073 down: no-op
-- The redirection is a code-only change (kg/kg_crdt.py). Reverting the
-- code requires no schema change. This file exists so the migration
-- down-coverage stays at 100% (Hard Rule 19: every numbered migration
-- has a .down.sql counterpart).
SELECT 1;
