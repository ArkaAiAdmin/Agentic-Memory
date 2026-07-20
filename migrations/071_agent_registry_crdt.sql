-- Migration 071: Agent registry CRDT table
-- Enables cross-agent discovery via sync (sync-based agent registry).
-- Each agent writes its identity to its own DB; sync propagates entries
-- between agents so memory_profile(action="agents") shows all peers.

CREATE TABLE IF NOT EXISTS agent_registry_crdt (
    agent_id TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    parent_agent TEXT DEFAULT '',
    namespace TEXT NOT NULL DEFAULT '',
    logical_clock INTEGER NOT NULL DEFAULT 0,
    version_vector TEXT NOT NULL DEFAULT '{}',
    last_seen REAL NOT NULL,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    PRIMARY KEY (agent_id, tenant_id)
);
