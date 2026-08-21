-- 079: UI-state inventory view for structured synthesis.
-- Materializes the "Step N Dropdown / Menu / Values:" observations emitted by
-- web-agent trajectories into a queryable (traj, step, vals, ctx) table with
-- an FTS5 index, so state/option questions can be answered by lookup instead
-- of lexical retrieval. Built by knowledge_graph/ui_state_inventory.py at
-- consolidation time; content mirrors source fact rows for idempotent rebuild.

CREATE TABLE IF NOT EXISTS ui_state_inventory (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    traj_id       TEXT NOT NULL,
    step          INTEGER NOT NULL,
    vals          TEXT NOT NULL,
    ctx           TEXT NOT NULL DEFAULT '',
    source_memory TEXT NOT NULL,
    built_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_uisi_traj_step ON ui_state_inventory(traj_id, step);
CREATE UNIQUE INDEX IF NOT EXISTS idx_uisi_source ON ui_state_inventory(source_memory);

-- Query-side index for the gated lookup phase (search orchestrator Phase 9.1).
CREATE VIRTUAL TABLE IF NOT EXISTS ui_state_inventory_fts USING fts5(
    vals,
    ctx,
    traj_id UNINDEXED,
    step UNINDEXED
);
