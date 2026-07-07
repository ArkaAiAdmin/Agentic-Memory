-- Rollback: remove audit_status columns added by 023_add_audit_status.sql
-- SQLite cannot DROP COLUMN in older versions, so we use the
-- backup-table pattern: CREATE new table without audit_status,
-- copy data, drop old, rename new.

DROP INDEX IF EXISTS idx_sessions_audit_status;
DROP INDEX IF EXISTS idx_decision_threads_audit_status;

-- sessions: recreate without audit_status
CREATE TABLE sessions_new (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    project_root TEXT,
    agent_id TEXT,
    parent_session_id TEXT REFERENCES sessions(id),
    summary_note_id TEXT REFERENCES memories(id),
    status TEXT DEFAULT 'active' CHECK (status IN ('active','compacted','ended','failed')),
    version_vector TEXT NOT NULL DEFAULT '{}',
    metadata JSON DEFAULT '{}' CHECK (json_valid(metadata))
);
INSERT INTO sessions_new SELECT id, started_at, ended_at, project_root, agent_id,
    parent_session_id, summary_note_id, status, version_vector, metadata
    FROM sessions;
DROP TABLE sessions;
ALTER TABLE sessions_new RENAME TO sessions;

-- decision_threads: recreate without audit_status
CREATE TABLE decision_threads_new (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    title TEXT NOT NULL,
    status TEXT DEFAULT 'open' CHECK (status IN ('open','resolved','superseded','deferred')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    superseded_by TEXT REFERENCES decision_threads(id),
    version_vector TEXT NOT NULL DEFAULT '{}',
    metadata JSON DEFAULT '{}' CHECK (json_valid(metadata))
);
INSERT INTO decision_threads_new SELECT id, session_id, title, status, created_at,
    resolved_at, superseded_by, version_vector, metadata
    FROM decision_threads;
DROP TABLE decision_threads;
ALTER TABLE decision_threads_new RENAME TO decision_threads;

-- thread_events: recreate without audit_status
CREATE TABLE thread_events_new (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES decision_threads(id),
    session_id TEXT NOT NULL REFERENCES sessions(id),
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('claim','evidence','decision','question','pivot')),
    content TEXT NOT NULL,
    content_summary TEXT DEFAULT '',
    memory_id TEXT REFERENCES memories(id),
    confidence REAL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
    created_at TEXT NOT NULL,
    version_vector TEXT NOT NULL DEFAULT '{}',
    UNIQUE(thread_id, seq)
);
INSERT INTO thread_events_new SELECT id, thread_id, session_id, seq, event_type,
    content, content_summary, memory_id, confidence, created_at, version_vector
    FROM thread_events;
DROP TABLE thread_events;
ALTER TABLE thread_events_new RENAME TO thread_events;

-- session_compaction_log: recreate without audit_status
CREATE TABLE session_compaction_log_new (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    compacted_at TEXT NOT NULL,
    tokens_before INTEGER,
    tokens_after INTEGER,
    summary_note_id TEXT REFERENCES memories(id),
    recovered_note_ids TEXT NOT NULL,
    metadata JSON DEFAULT '{}' CHECK (json_valid(metadata)),
    version_vector TEXT NOT NULL DEFAULT '{}'
);
INSERT INTO session_compaction_log_new SELECT id, session_id, compacted_at,
    tokens_before, tokens_after, summary_note_id, recovered_note_ids,
    metadata, version_vector
    FROM session_compaction_log;
DROP TABLE session_compaction_log;
ALTER TABLE session_compaction_log_new RENAME TO session_compaction_log;

-- Recreate indexes from 022 (audit_status indexes are not recreated)
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_root, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_threads_session ON decision_threads(session_id, status);
CREATE INDEX IF NOT EXISTS idx_thread_events_thread ON thread_events(thread_id, seq);
CREATE INDEX IF NOT EXISTS idx_compaction_session ON session_compaction_log(session_id);
