-- Migration 006 DOWN: drop the CHECK constraint and new indexes.
-- Note: SQLite cannot drop a CHECK constraint from a table without
-- rebuilding. We rename-and-recreate, restoring the prior schema.
-- This is destructive; the down path is for emergency rollback only.

-- Drop new indexes
DROP INDEX IF EXISTS idx_shared_memories_shared_at;
DROP INDEX IF EXISTS idx_backlinks_source_id;
DROP INDEX IF EXISTS idx_memories_active;

-- Restore task_queue without CHECK constraint
CREATE TABLE IF NOT EXISTS task_queue_old (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    payload TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    source_note_id TEXT
);
INSERT OR IGNORE INTO task_queue_old
    SELECT id, task_type, payload, status, priority, created_at, started_at,
           completed_at, error, attempts, max_attempts, source_note_id
    FROM task_queue;
DROP TABLE task_queue;
ALTER TABLE task_queue_old RENAME TO task_queue;
CREATE INDEX IF NOT EXISTS idx_task_queue_status ON task_queue(status);
CREATE INDEX IF NOT EXISTS idx_task_queue_task_type ON task_queue(task_type);
CREATE INDEX IF NOT EXISTS idx_task_queue_priority ON task_queue(priority DESC, created_at ASC);
