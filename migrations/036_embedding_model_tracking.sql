-- Migration 036: Track embedding model_id in memory_vec_idx (C4.3)
-- Allows drift detection: if the user changes the embedding model,
-- the search path can detect that the persisted index was built with
-- a different model and invalidate/reject it.

ALTER TABLE memory_vec_idx ADD COLUMN model_id TEXT DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_vec_idx_model
    ON memory_vec_idx(model_id)
    WHERE model_id != '';
