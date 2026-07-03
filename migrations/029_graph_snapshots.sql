CREATE TABLE IF NOT EXISTS graph_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at REAL NOT NULL,
    entity_count INTEGER,
    edge_count INTEGER,
    community_count INTEGER,
    avg_centrality REAL,
    top_entities TEXT,        -- JSON: [{name, centrality}]
    new_entities TEXT,        -- JSON: names added since last snapshot
    removed_entities TEXT     -- JSON: names removed since last snapshot
);
CREATE INDEX IF NOT EXISTS idx_graph_snapshots_captured_at
    ON graph_snapshots(captured_at);
