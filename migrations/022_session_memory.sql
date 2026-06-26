-- Session Memory System v22
-- Tables: sessions, decision_threads, thread_events, session_compaction_log
-- All include version_vector for CRDT merge semantics
-- Metadata columns have json_valid CHECK constraints
-- Thread events have both thread_id and session_id FK

CREATE TABLE sessions (
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

CREATE TABLE decision_threads (
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

CREATE TABLE thread_events (
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

CREATE TABLE session_compaction_log (
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

CREATE INDEX idx_sessions_project ON sessions(project_root, started_at DESC);
CREATE INDEX idx_threads_session ON decision_threads(session_id, status);
CREATE INDEX idx_thread_events_thread ON thread_events(thread_id, seq);
CREATE INDEX idx_compaction_session ON session_compaction_log(session_id);
