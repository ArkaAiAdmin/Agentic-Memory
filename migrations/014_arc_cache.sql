-- Migration 014: P0 fix #4 — wire ARCCache into the eviction path
--
-- The arc_ghosts and arc_stats tables are defined inside arc_cache.py
-- via a lazy CREATE TABLE IF NOT EXISTS, but that lazy init only fires
-- when the ARCCache class is actually instantiated. The live DB never
-- reaches that path because the eviction layer (tier_migration) doesn't
-- import arc_cache, and memory_arc_stats is a print-and-return tool.
--
-- This migration is the canonical schema. The lazy init in arc_cache.py
-- stays as a safety net (CREATE TABLE IF NOT EXISTS) so the module can
-- be unit-tested with tempfiles and so any caller that opens ARCCache
-- against an un-migrated DB still gets a working surface.
--
-- Tables:
--   arc_ghosts — one row per memory that was evicted. Acts as the B1/B2
--     ghost lists in the ARC algorithm. would_have_been_hit flips to 1
--     the next time the memory is needed but no longer in cache.
--   arc_stats — a small key/value table (eviction_pressure, ghost_hit_rate,
--     total_ghosts, last_eviction_at, last_recent_at) so the MCP tool can
--     read state without recomputing.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS arc_ghosts (
    memory_id TEXT PRIMARY KEY,
    evicted_at TEXT NOT NULL,
    tier TEXT NOT NULL,
    would_have_been_hit INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS arc_stats (
    key TEXT PRIMARY KEY,
    value REAL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_arc_ghosts_evicted_at ON arc_ghosts(evicted_at);
