-- Migration 027: memory_revision_log table — audit trail for agent self-editing
--
-- Tracks every supersede, delete, amend, revert, and retract action
-- with old/new content snapshots and rationale capture.
--
-- The metadata column stores revision-specific JSON payloads:
--   amend: {"additions": [...], "deletions": [...]}
--   supersede: {"superseded_by": "<new_id>"}
--   revert: {"previous_superseded_by": "<old_new_id>"}

CREATE TABLE IF NOT EXISTS memory_revision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    revision_type TEXT NOT NULL,     -- supersede | delete | amend | revert | retract
    old_content TEXT,
    new_content TEXT,
    rationale TEXT,
    metadata TEXT,                   -- revision-specific JSON payload
    agent_id TEXT,
    created_at REAL
);

CREATE INDEX IF NOT EXISTS idx_revision_log_memory ON memory_revision_log(memory_id);
CREATE INDEX IF NOT EXISTS idx_revision_log_type ON memory_revision_log(revision_type);
CREATE INDEX IF NOT EXISTS idx_revision_log_created ON memory_revision_log(created_at);
