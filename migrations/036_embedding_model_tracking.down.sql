-- Down-migration 036: Remove model_id tracking from memory_vec_idx.
-- SQLite < 3.35.2 doesn't support DROP COLUMN, so we drop and recreate
-- the table with the original schema (including CHECK (id = 1)).

DROP INDEX IF EXISTS idx_vec_idx_model;

DROP TABLE IF EXISTS memory_vec_idx;

CREATE TABLE IF NOT EXISTS memory_vec_idx (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    n_vectors         INTEGER NOT NULL,
    dim               INTEGER NOT NULL,
    metric            TEXT    NOT NULL,
    quantization      TEXT    NOT NULL,
    connectivity      INTEGER NOT NULL,
    expansion_add     INTEGER NOT NULL,
    expansion_search  INTEGER NOT NULL,
    built_at          REAL    NOT NULL,
    index_blob        BLOB    NOT NULL,
    key_count         INTEGER NOT NULL
);
