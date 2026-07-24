-- Migration 005: Columns, indexes, chunks, FTS5 porter
-- Combines: _migrate_ensure_columns, _migrate_ensure_indexes,
-- _migrate_ensure_chunks_table, _migrate_fts5_porter_tokenizer,
-- _migrate_ensure_fts_triggers

-- === Columns ===
-- NOTE: On a fresh DB, migration 000 already creates the memories table
-- with ALL these columns.  The ALTER TABLE ADD COLUMN statements below
-- will produce "duplicate column name" warnings — this is expected and
-- harmless.  The migration runner catches them as idempotent skips.
-- On older DBs (pre-000) these statements add the missing columns.
-- There is no SQLite equivalent of "ADD COLUMN IF NOT EXISTS".
ALTER TABLE memories ADD COLUMN valid_from TEXT;
ALTER TABLE memories ADD COLUMN valid_to TEXT;
ALTER TABLE memories ADD COLUMN superseded_by TEXT;
ALTER TABLE memories ADD COLUMN last_accessed TEXT;
ALTER TABLE memories ADD COLUMN deleted_at TEXT;
ALTER TABLE memories ADD COLUMN deleted_by TEXT;
ALTER TABLE memories ADD COLUMN context_prefix TEXT;
ALTER TABLE memories ADD COLUMN category TEXT;
ALTER TABLE memories ADD COLUMN tier TEXT;
ALTER TABLE memories ADD COLUMN importance_score REAL;
ALTER TABLE memories ADD COLUMN metadata TEXT;
ALTER TABLE memories ADD COLUMN repo_id TEXT;
ALTER TABLE memories ADD COLUMN consolidation_state TEXT DEFAULT 'working';
ALTER TABLE memories ADD COLUMN pinned INTEGER DEFAULT 0;
ALTER TABLE memories ADD COLUMN importance INTEGER DEFAULT 3;
ALTER TABLE memories ADD COLUMN decay TEXT DEFAULT 'none';
ALTER TABLE memories ADD COLUMN score REAL DEFAULT 1.0;
ALTER TABLE memories ADD COLUMN supersedes TEXT;
ALTER TABLE memories ADD COLUMN access_count INTEGER DEFAULT 1;
ALTER TABLE memories ADD COLUMN success_score REAL DEFAULT 0.0;
ALTER TABLE memories ADD COLUMN fitness_score REAL DEFAULT 1.0;
ALTER TABLE memories ADD COLUMN conflict_policy TEXT DEFAULT 'supersede';
ALTER TABLE memories ADD COLUMN version_vector TEXT DEFAULT '{}';
ALTER TABLE memories ADD COLUMN logical_clock INTEGER DEFAULT 0;
ALTER TABLE memories ADD COLUMN observed_at TEXT;
ALTER TABLE memories ADD COLUMN content TEXT;
ALTER TABLE memories ADD COLUMN source_file TEXT;
ALTER TABLE memories ADD COLUMN tags TEXT DEFAULT '[]';
ALTER TABLE memories ADD COLUMN created_at TEXT;
ALTER TABLE memories ADD COLUMN updated_at TEXT;

-- === Indexes ===
CREATE INDEX IF NOT EXISTS idx_memories_repo_id ON memories(repo_id);
CREATE INDEX IF NOT EXISTS idx_memories_pinned ON memories(pinned);
CREATE INDEX IF NOT EXISTS idx_memories_consolidation_state ON memories(consolidation_state);
CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at);
CREATE INDEX IF NOT EXISTS idx_memories_updated_at ON memories(updated_at);
CREATE INDEX IF NOT EXISTS idx_memories_observed_at ON memories(observed_at);
CREATE INDEX IF NOT EXISTS idx_memories_fitness_score ON memories(fitness_score);
CREATE INDEX IF NOT EXISTS idx_memories_source_file ON memories(source_file);
CREATE INDEX IF NOT EXISTS idx_memories_valid_to ON memories(valid_to);
CREATE INDEX IF NOT EXISTS idx_memories_valid_from ON memories(valid_from);
CREATE INDEX IF NOT EXISTS idx_memories_superseded_by ON memories(superseded_by);
CREATE INDEX IF NOT EXISTS idx_memories_last_accessed ON memories(last_accessed);
CREATE INDEX IF NOT EXISTS idx_memories_deleted_at ON memories(deleted_at);

-- === Chunks table ===
CREATE TABLE IF NOT EXISTS memory_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id TEXT NOT NULL,
    chunk_idx INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(parent_id, chunk_idx),
    FOREIGN KEY (parent_id) REFERENCES memories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_memory_chunks_parent_id ON memory_chunks(parent_id);

-- FTS5 virtual table for chunk-level search (created separately to handle virtual table)
-- Note: This is a no-op if memory_chunks_fts already exists

-- === FTS5 sync triggers ===
-- Triggers to keep memories_fts in sync (created if memories_fts exists)

-- Note: FTS5 porter tokenizer rebuild and trigger creation are handled
-- by the Python _migrate_fts5_porter_tokenizer and _migrate_ensure_fts_triggers
-- functions in memory_common.py, as they require dynamic SQL generation.
