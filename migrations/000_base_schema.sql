-- Migration 000: Base schema (memories table + KG tables)
-- This is the new baseline migration. It replaces the inline
-- CREATE TABLE blocks that were previously in migration_runner.py.
-- Idempotent: IF NOT EXISTS guards allow safe re-runs on existing
-- databases that already have these tables.

CREATE TABLE IF NOT EXISTS memories (
    id                  TEXT PRIMARY KEY,
    content             TEXT NOT NULL,
    source_file         TEXT NOT NULL,
    tags                TEXT DEFAULT '[]',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    observed_at         TEXT NOT NULL,
    pinned              INTEGER DEFAULT 0,
    importance          INTEGER DEFAULT 3,
    decay               TEXT DEFAULT 'none',
    score               REAL DEFAULT 1.0,
    supersedes          TEXT,
    repo_id             TEXT,
    access_count        INTEGER DEFAULT 1,
    success_score       REAL DEFAULT 0.0,
    fitness_score       REAL DEFAULT 1.0,
    conflict_policy     TEXT DEFAULT 'supersede',
    version_vector      TEXT DEFAULT '{}',
    logical_clock       INTEGER DEFAULT 0,
    consolidation_state TEXT DEFAULT 'working'
);

CREATE TABLE IF NOT EXISTS kg_entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    entity_type TEXT,
    mentions    INTEGER DEFAULT 1,
    created_at  TEXT,
    updated_at  TEXT,
    UNIQUE(name, entity_type)
);

CREATE INDEX IF NOT EXISTS idx_kg_entities_name ON kg_entities(name);
CREATE INDEX IF NOT EXISTS idx_kg_entities_type ON kg_entities(entity_type);

CREATE TABLE IF NOT EXISTS kg_edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
    target_id   INTEGER NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
    relation    TEXT NOT NULL DEFAULT 'related_to',
    weight      REAL DEFAULT 1.0,
    created_at  TEXT,
    valid_at    TEXT,
    invalid_at  TEXT,
    UNIQUE(source_id, target_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_kg_edges_source ON kg_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_kg_edges_target ON kg_edges(target_id);
CREATE INDEX IF NOT EXISTS idx_kg_edges_relation ON kg_edges(relation);
CREATE INDEX IF NOT EXISTS idx_kg_edges_valid_at ON kg_edges(valid_at);
CREATE INDEX IF NOT EXISTS idx_kg_edges_invalid_at ON kg_edges(invalid_at);

CREATE TABLE IF NOT EXISTS kg_facts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    subject             TEXT NOT NULL,
    predicate           TEXT NOT NULL,
    object              TEXT NOT NULL,
    confidence          REAL DEFAULT 1.0,
    locked              INTEGER DEFAULT 0,
    first_seen          REAL,
    last_seen           REAL,
    mention_count       INTEGER DEFAULT 1,
    source_memory       TEXT,
    context             TEXT,
    UNIQUE(subject, predicate, object)
);

CREATE INDEX IF NOT EXISTS idx_kg_facts_subject ON kg_facts(subject);
CREATE INDEX IF NOT EXISTS idx_kg_facts_predicate ON kg_facts(predicate);
CREATE INDEX IF NOT EXISTS idx_kg_facts_object ON kg_facts(object);
CREATE INDEX IF NOT EXISTS idx_kg_facts_spo ON kg_facts(subject, predicate, object);

CREATE VIRTUAL TABLE kg_entities_fts USING fts5(
    name, entity_type, content='kg_entities', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS kg_entities_fts_ai AFTER INSERT ON kg_entities BEGIN
    INSERT INTO kg_entities_fts(rowid, name, entity_type)
    VALUES (new.id, new.name, new.entity_type); END;
CREATE TRIGGER IF NOT EXISTS kg_entities_fts_ad AFTER DELETE ON kg_entities BEGIN
    INSERT INTO kg_entities_fts(kg_entities_fts, rowid, name, entity_type)
    VALUES ('delete', old.id, old.name, old.entity_type); END;
CREATE TRIGGER IF NOT EXISTS kg_entities_fts_au AFTER UPDATE ON kg_entities BEGIN
    INSERT INTO kg_entities_fts(kg_entities_fts, rowid, name, entity_type)
    VALUES ('delete', old.id, old.name, old.entity_type);
    INSERT INTO kg_entities_fts(rowid, name, entity_type)
    VALUES (new.id, new.name, new.entity_type); END;

INSERT INTO kg_entities_fts(rowid, name, entity_type)
SELECT id, name, entity_type FROM kg_entities
WHERE NOT EXISTS (SELECT 1 FROM kg_entities_fts);

-- === Backlinks (wiki-style bidirectional links) ===
CREATE TABLE IF NOT EXISTS backlinks (
    source_id TEXT,
    target_id TEXT,
    PRIMARY KEY (source_id, target_id)
);

-- === Shared memories (cross-agent pool) ===
CREATE TABLE IF NOT EXISTS shared_memories (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT,
    tags TEXT,
    shared_at REAL NOT NULL,
    source_note_id TEXT,
    metadata TEXT,
    target_agent_id TEXT DEFAULT NULL,
    shared_with TEXT DEFAULT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default'
);

-- === User profile access log ===
CREATE TABLE IF NOT EXISTS user_profile_access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id TEXT NOT NULL,
    source TEXT DEFAULT 'search',
    category TEXT,
    tags TEXT,
    accessed_at REAL NOT NULL
);

-- === Search phase stats ===
CREATE TABLE IF NOT EXISTS search_phase_stats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id    TEXT NOT NULL,
    phase_name  TEXT NOT NULL,
    latency_ms  REAL NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_search_phase_stats_query ON search_phase_stats(query_id, created_at);

-- === File modification times (incremental backfill tracking) ===
CREATE TABLE IF NOT EXISTS file_mtimes (
    path TEXT PRIMARY KEY,
    mtime REAL NOT NULL,
    content_hash TEXT NOT NULL
);

-- === User access log (adaptive retention) ===
CREATE TABLE IF NOT EXISTS user_access_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id    TEXT    NOT NULL,
    access_ts  REAL    NOT NULL,
    source     TEXT    NOT NULL DEFAULT 'unknown'
);
CREATE INDEX IF NOT EXISTS idx_user_access_note ON user_access_log(note_id);

-- === Dead letter messages (coordination) ===
CREATE TABLE IF NOT EXISTS dead_letter_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    original_id INTEGER,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    message_type TEXT NOT NULL,
    payload TEXT,
    status TEXT DEFAULT 'dead',
    created_at REAL NOT NULL,
    dead_lettered_at REAL NOT NULL,
    reason TEXT
);

-- === Answer rerank cache ===
CREATE TABLE IF NOT EXISTS answer_rerank_cache (
    memory_id   TEXT NOT NULL,
    query_hash  TEXT NOT NULL,
    score       REAL NOT NULL,
    snippet     TEXT NOT NULL,
    created_at  REAL NOT NULL DEFAULT (unixepoch()),
    UNIQUE (memory_id, query_hash)
);
CREATE INDEX IF NOT EXISTS idx_arc_memory ON answer_rerank_cache(memory_id);

-- === Review schedule (SM-2 spaced repetition) ===
CREATE TABLE IF NOT EXISTS review_schedule (
    memory_id TEXT PRIMARY KEY,
    retrieval_count INTEGER DEFAULT 0,
    interval_days REAL DEFAULT 1.0,
    next_review TEXT NOT NULL,
    last_reviewed TEXT,
    ease_factor REAL DEFAULT 2.5
);

-- === Saga log (transaction audit trail) ===
CREATE TABLE IF NOT EXISTS saga_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    saga_id TEXT NOT NULL,
    saga_name TEXT NOT NULL,
    step_idx INTEGER NOT NULL,
    step_name TEXT NOT NULL,
    status TEXT NOT NULL,
    ts REAL NOT NULL
);
