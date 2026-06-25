-- Migration 014 down: drop arc_ghosts and arc_stats tables.
-- Reverses migrations/014_arc_cache.sql.

DROP INDEX IF EXISTS idx_arc_ghosts_evicted_at;
DROP TABLE IF EXISTS arc_stats;
DROP TABLE IF EXISTS arc_ghosts;
