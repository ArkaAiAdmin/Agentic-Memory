-- 040_belief_review_queue.sql
-- G4: Queue table for automatic belief-review cadence.
CREATE TABLE IF NOT EXISTS belief_review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    belief_id INTEGER REFERENCES belief_assertions(id) ON DELETE CASCADE,
    fact_id INTEGER,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL,
    reviewed_at REAL
);
