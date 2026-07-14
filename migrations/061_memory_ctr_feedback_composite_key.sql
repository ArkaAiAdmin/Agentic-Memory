-- P2a fix (FIX 2, agentic-memory search-pipeline review): let memory_ctr_feedback
-- hold one row per (query_id, result) so CTR click/dismiss signals can be
-- correlated back to the impression that produced them.
--
-- The previous schema used `id TEXT PRIMARY KEY` and _record_search_telemetry
-- always wrote a single sentinel row (id='__search__'), so only ONE impression
-- could ever exist and compute_channel_weights never accumulated any signal
-- (it needs >=10 distinct query groups with click/dismiss data).
--
-- Rebuild to a composite primary key (query_id, id). On a fresh DB the old
-- table does not yet exist when this runs (the Python safety-net that creates
-- it executes AFTER the numbered migrations), so the INSERT/SELECT from the
-- old table raises "no such table" — the migration runner treats that as an
-- idempotent forward-reference and skips it. On an existing DB the old single-
-- PK table is copied, dropped, and renamed into the new shape.
CREATE TABLE IF NOT EXISTS memory_ctr_feedback_new (
    query_id      TEXT NOT NULL,
    id            TEXT NOT NULL,
    returned_at   REAL NOT NULL,
    clicked_at    REAL,
    dismissed_at  REAL,
    source        TEXT,
    ranking_params TEXT,
    PRIMARY KEY (query_id, id)
);
INSERT INTO memory_ctr_feedback_new (query_id, id, returned_at, clicked_at, dismissed_at, source, ranking_params)
    SELECT COALESCE(query_id, '__unknown__'), id, returned_at, clicked_at, dismissed_at, source, ranking_params
    FROM memory_ctr_feedback;
DROP TABLE IF EXISTS memory_ctr_feedback;
ALTER TABLE memory_ctr_feedback_new RENAME TO memory_ctr_feedback;
CREATE INDEX IF NOT EXISTS idx_ctr_query_id ON memory_ctr_feedback(query_id);
