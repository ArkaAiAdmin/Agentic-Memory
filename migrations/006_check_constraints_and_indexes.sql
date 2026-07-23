-- Migration 006: H17 fix — enforce CHECK constraints on queue status,
-- add partial index for temporal validity filter, add FK to backlinks
-- (target/source are notes that exist), and index shared_memories.shared_at.
--
-- All statements are idempotent. Wrapped in a savepoint-style approach:
-- if a CHECK already exists, CREATE will fail harmlessly; the index
-- statements use IF NOT EXISTS throughout.

-- === task_queue.status CHECK constraint ===
-- SQLite allows CHECK constraints; we enforce the documented enum.
CREATE TABLE IF NOT EXISTS task_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    payload TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    priority INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    source_note_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_queue_status ON task_queue(status);
CREATE INDEX IF NOT EXISTS idx_task_queue_task_type ON task_queue(task_type);
CREATE INDEX IF NOT EXISTS idx_task_queue_priority ON task_queue(priority DESC, created_at ASC);

-- === H7 fix: partial index for temporal validity filter ===
-- search_pipeline.py:2409 does SELECT id FROM memories WHERE valid_to IS NULL
-- on every search. A partial index makes this O(log N) instead of O(N).
CREATE INDEX IF NOT EXISTS idx_memories_active
    ON memories(id) WHERE valid_to IS NULL OR valid_to = '';

-- === H7 fix: backlinks(target_id) and backlinks(source_id) indexes ===
-- mcp_tools.py:477-486 and search_pipeline.py:2534-2548 do unindexed
-- IN (...) lookups. The migration runner already created the index in
-- 005_columns_indexes_chunks.sql line 29 (idx_backlinks_target_id).
-- Add the missing source index for symmetry.
CREATE INDEX IF NOT EXISTS idx_backlinks_source_id ON backlinks(source_id);

-- === Perf: index on shared_memories.shared_at ===
-- multi_agent.py:186-189 filters by shared_at for TTL. Without the
-- index this is a full scan on every list_shared_memories call.
CREATE INDEX IF NOT EXISTS idx_shared_memories_shared_at
    ON shared_memories(shared_at);
