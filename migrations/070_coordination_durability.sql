-- Migration 070: Coordination durability tables
-- Adds coordination_audit and agent_heartbeats for crash recovery

CREATE TABLE IF NOT EXISTS coordination_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    target TEXT,
    detail TEXT,
    timestamp REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coordination_audit_action ON coordination_audit(action, timestamp);
CREATE INDEX IF NOT EXISTS idx_coordination_audit_agent ON coordination_audit(agent_id, timestamp);

CREATE TABLE IF NOT EXISTS agent_heartbeats (
    agent_id TEXT PRIMARY KEY,
    last_heartbeat REAL NOT NULL,
    session_id TEXT,
    project_id TEXT
);
