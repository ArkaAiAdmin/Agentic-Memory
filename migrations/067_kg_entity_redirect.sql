-- 067_kg_entity_redirect.sql
-- Durable entity redirect map (Sprint 2.4).
--
-- When name/fingerprint collisions are resolved during CRDT projection,
-- losing entity_ids are mapped to a winning entity_id.  The redirect map
-- is otherwise only held in memory and recomputed on every projection,
-- which means queries that resolve a stale/loser entity_id outside the
-- projection path (e.g. edges written before a later merge, or external
-- lookups) cannot find the live winner.  Persisting the map makes the
-- loser->winner resolution durable and queryable.
--
-- Additive only; primary key keeps the table idempotent across re-runs.

CREATE TABLE IF NOT EXISTS kg_entity_redirect (
    loser_id    INTEGER NOT NULL,
    winner_id   INTEGER NOT NULL,
    reason      TEXT    DEFAULT 'collision',
    created_at  TEXT    DEFAULT (datetime('now')),
    tenant_id   TEXT    DEFAULT '',
    PRIMARY KEY (loser_id, winner_id)
);

CREATE INDEX IF NOT EXISTS idx_kg_entity_redirect_winner
    ON kg_entity_redirect(winner_id);
CREATE INDEX IF NOT EXISTS idx_kg_entity_redirect_tenant
    ON kg_entity_redirect(tenant_id);
