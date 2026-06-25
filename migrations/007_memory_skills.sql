-- Migration 007: H22+ — add memory_skills table for the skill agent
-- (procedural knowledge cache).
--
-- Skills are extracted from procedural memories (numbered steps, code blocks,
-- action-verb headers) by cron_skill_extraction.py and stored here for
-- cheap, trigger-token lookup that bypasses the full RAG pipeline.
--
-- Mirrors the schema in skill_extractor.py:50-66 (kept in sync — the
-- CREATE TABLE IF NOT EXISTS in ensure_skill_schema() is a no-op once this
-- migration has run, so it's safe to keep the bootstrap there too).
--
-- Idempotent: CREATE TABLE IF NOT EXISTS, all indexes use IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS memory_skills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    source_memory_id TEXT,
    topic           TEXT,
    description     TEXT,
    triggers        TEXT DEFAULT '[]',
    steps           TEXT DEFAULT '[]',
    content_hash    TEXT,
    hit_count       INTEGER DEFAULT 0,
    last_used_at    REAL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_skills_topic ON memory_skills(topic);
CREATE INDEX IF NOT EXISTS idx_memory_skills_hit ON memory_skills(hit_count DESC);
