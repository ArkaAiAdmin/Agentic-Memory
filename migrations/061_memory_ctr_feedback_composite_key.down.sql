-- Roll back migration 061: collapse the composite (query_id, id) key back to
-- the legacy single-column `id` primary key. The per-memory click/dismiss
-- signal is preserved (latest value per id via aggregates); per-query grouping
-- is lost, restoring the pre-061 (dead-CTR) behaviour.
CREATE TABLE IF NOT EXISTS memory_ctr_feedback_old (
    id TEXT PRIMARY KEY,
    query_id TEXT NOT NULL,
    returned_at REAL NOT NULL,
    clicked_at REAL,
    dismissed_at REAL,
    source TEXT,
    ranking_params TEXT
);
INSERT INTO memory_ctr_feedback_old (id, query_id, returned_at, clicked_at, dismissed_at, source, ranking_params)
    SELECT id, query_id, MAX(returned_at), MAX(clicked_at), MAX(dismissed_at), MAX(source), MAX(ranking_params)
    FROM memory_ctr_feedback
    GROUP BY id;
DROP TABLE IF EXISTS memory_ctr_feedback;
ALTER TABLE memory_ctr_feedback_old RENAME TO memory_ctr_feedback;
CREATE INDEX IF NOT EXISTS idx_ctr_query_id ON memory_ctr_feedback(query_id);
