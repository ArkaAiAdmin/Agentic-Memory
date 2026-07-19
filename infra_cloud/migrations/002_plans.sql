-- Migration 002: Plans, subscriptions references, and usage metering
-- Anchor this migration in the cloud_state database schema.

CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    max_storage_mb INTEGER NOT NULL,
    max_mcp_calls_per_day INTEGER NOT NULL,
    max_seats INTEGER NOT NULL,
    retention_days INTEGER NOT NULL,
    features_json TEXT
);

INSERT OR IGNORE INTO plans (id, name, max_storage_mb, max_mcp_calls_per_day, max_seats, retention_days, features_json) VALUES
('free', 'Free Tier', 50, 1000, 1, 7, '{}'),
('pro', 'Pro Tier', 1000, 100000, 10, 90, '{}'),
('enterprise', 'Enterprise Tier', 50000, 999999999, 100, 365, '{}');

CREATE TABLE IF NOT EXISTS usage_records (
    deployment_id TEXT NOT NULL,
    day TEXT NOT NULL, -- 'YYYY-MM-DD'
    mcp_calls INTEGER DEFAULT 0,
    rest_calls INTEGER DEFAULT 0,
    storage_bytes INTEGER DEFAULT 0,
    PRIMARY KEY (deployment_id, day),
    FOREIGN KEY (deployment_id) REFERENCES deployments(deployment_id) ON DELETE CASCADE
);
