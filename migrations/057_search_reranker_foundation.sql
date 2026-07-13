-- 057: Search-pipeline SOTA — Phase 0 foundations.
--
-- Creates the three foundation tables consumed by later phases of the
-- search reranker work and seeds the per-category temporal decay priors
-- that Phase 2 consumes:
--
--   * memory_search_interaction  — single source of truth for CTR signals
--       (impression / click / used_in_response / dismissed) keyed by
--       (query_id, memory_id, action). This is the production table that
--       the CTR producer writes to; recall/recall.py and orchestrator.py
--       read from it. Reference code self-creates it in tests, but in
--       production it must come from this migration.
--   * memory_query_type_stats    — per-query-type learned rerank weights
--       (populated by Phase 6).
--   * memory_temporal_priors     — per-category decay half-lives (Phase 2
--       consumes). Seeded here with sensible defaults so Phase 2 has a
--       baseline to read from day one.
--
-- Additive migration: every object uses IF NOT EXISTS / INSERT OR IGNORE
-- so re-applying is a no-op. The rollback (057.down.sql) drops the three
-- tables; the seeded temporal_priors rows vanish with the table, which is
-- acceptable for a down migration (the priors are static defaults
-- regenerated on any future re-apply).

CREATE TABLE IF NOT EXISTS memory_search_interaction (
    id          INTEGER PRIMARY KEY,
    query_id    TEXT NOT NULL,
    memory_id   TEXT NOT NULL,
    action      TEXT NOT NULL,   -- 'impression' | 'click' | 'used_in_response' | 'dismissed'
    tenant_id   TEXT NOT NULL DEFAULT 'default',
    rank        INTEGER,
    ts          REAL NOT NULL DEFAULT (unixepoch()),
    UNIQUE (query_id, memory_id, action)
);
CREATE INDEX IF NOT EXISTS idx_msi_query   ON memory_search_interaction(query_id);
CREATE INDEX IF NOT EXISTS idx_msi_memory  ON memory_search_interaction(memory_id);
CREATE INDEX IF NOT EXISTS idx_msi_action  ON memory_search_interaction(action);

-- memory_query_type_stats: per-query-type learned rerank weights (Phase 6 populates).
CREATE TABLE IF NOT EXISTS memory_query_type_stats (
    query_type   TEXT PRIMARY KEY,
    weights_json TEXT NOT NULL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    updated_at   REAL NOT NULL DEFAULT (unixepoch())
);

-- memory_temporal_priors: per-category decay half-lives (Phase 2 consumes).
CREATE TABLE IF NOT EXISTS memory_temporal_priors (
    category       TEXT PRIMARY KEY,
    half_life_days REAL NOT NULL,
    updated_at     REAL NOT NULL DEFAULT (unixepoch())
);

-- Seed default temporal priors (Phase 2 baseline decay half-lives).
INSERT OR IGNORE INTO memory_temporal_priors (category, half_life_days) VALUES
    ('lessons', 180),
    ('concepts', 730),
    ('sessions', 14),
    ('preferences', 90),
    ('projects', 365),
    ('decisions', 365),
    ('facts', 90);
