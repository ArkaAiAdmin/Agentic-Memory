-- 012_kg_extraction_stats.sql
-- P2a.2: per-memory observability for the KG extraction pipeline.
--
-- Tracks how many entities each memory produced, how many came from
-- the regex path vs the LLM fallback, how long extraction took, and
-- any error message. Powers the "is the KG actually working?" check
-- that the previous silent try/except swallowed.
--
-- All statements are idempotent. The table is also created defensively
-- inside knowledge_graph.ensure_kg_schema() so a fresh DB that has
-- not yet run this migration still gets the table when the KG is
-- enabled.

CREATE TABLE IF NOT EXISTS kg_extraction_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    entities_extracted INTEGER DEFAULT 0,
    regex_count INTEGER DEFAULT 0,
    llm_count INTEGER DEFAULT 0,
    duration_ms REAL DEFAULT 0,
    error TEXT,
    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_kg_extraction_stats_memory
    ON kg_extraction_stats(memory_id);

CREATE INDEX IF NOT EXISTS idx_kg_extraction_stats_created
    ON kg_extraction_stats(created_at);
