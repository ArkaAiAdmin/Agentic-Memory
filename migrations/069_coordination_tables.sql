-- Migration 069: Multi-agent coordination tables
-- Adds shared_tasks, project_state, agent_messages, file_locks

-- 1. Shared task board
CREATE TABLE IF NOT EXISTS shared_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    description TEXT,
    assigned_to TEXT,
    status TEXT DEFAULT 'pending',
    created_by TEXT NOT NULL,
    created_at REAL,
    updated_at REAL,
    depends_on INTEGER REFERENCES shared_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_shared_tasks_project ON shared_tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_shared_tasks_status ON shared_tasks(status);
CREATE INDEX IF NOT EXISTS idx_shared_tasks_assigned ON shared_tasks(assigned_to);

-- 2. Shared project state
CREATE TABLE IF NOT EXISTS project_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    updated_by TEXT NOT NULL,
    updated_at REAL,
    UNIQUE(project_id, key)
);
CREATE INDEX IF NOT EXISTS idx_project_state_project ON project_state(project_id);

-- 3. Agent messaging
CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    message_type TEXT NOT NULL,
    payload TEXT,
    status TEXT DEFAULT 'pending',
    created_at REAL,
    delivered_at REAL
);
CREATE INDEX IF NOT EXISTS idx_agent_messages_to ON agent_messages(to_agent, status);
CREATE INDEX IF NOT EXISTS idx_agent_messages_from ON agent_messages(from_agent);

-- 4. File locks
CREATE TABLE IF NOT EXISTS file_locks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    locked_by TEXT NOT NULL,
    locked_at REAL,
    expires_at REAL,
    UNIQUE(file_path)
);
CREATE INDEX IF NOT EXISTS idx_file_locks_path ON file_locks(file_path);
