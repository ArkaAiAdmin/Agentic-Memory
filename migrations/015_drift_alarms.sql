-- 015_drift_alarms.sql
-- v15: drift_alarms table for tracking concept-drift alarms.
--
-- The AGENTS.md docs say the system has 25 user-visible tables
-- (including "drift alarms"), but no drift_alarms table existed.
-- This migration adds it. Concept drift was previously tracked
-- only in `concept_drift` (a single stream of centroid-vs-centroid
-- distance events), with no per-memory attribution, no severity
-- tiers, and no acknowledgement workflow.
--
-- Schema:
--   * memory_id: which memory triggered the alarm (FK to memories)
--   * concept: short label for the drifted concept (e.g. "embedding centroid",
--     "embedding_dim_42", or the dimension name for per-dimension alarms)
--   * drift_score: cosine distance or similar (0.0 = no drift, 1.0 = orthogonal)
--   * threshold: the threshold value at the time of detection (snapshot)
--   * alarm_level: severity tier. info < warning < critical.
--     - info:     drift_score just above threshold (10% over)
--     - warning:  drift_score notably above threshold (50% over)
--     - critical: drift_score far above threshold (2x+ over)
--   * detected_at: ISO-8601 UTC timestamp of detection
--   * acknowledged_at/acknowledged_by/notes: operator workflow fields
--
-- Indexes:
--   * idx_drift_alarms_memory: per-memory alarm history
--   * idx_drift_alarms_detected: chronological scan (newest first)
--   * idx_drift_alarms_unack:    partial index of unacknowledged alarms
--     (powers the operator's "what needs my attention?" dashboard query)

CREATE TABLE IF NOT EXISTS drift_alarms (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id       TEXT    NOT NULL,
    concept         TEXT    NOT NULL,
    drift_score     REAL    NOT NULL,
    threshold       REAL    NOT NULL,
    alarm_level     TEXT    NOT NULL CHECK(alarm_level IN ('info', 'warning', 'critical')),
    detected_at     TEXT    NOT NULL,
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    notes           TEXT,
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_drift_alarms_memory
    ON drift_alarms(memory_id);

CREATE INDEX IF NOT EXISTS idx_drift_alarms_detected
    ON drift_alarms(detected_at DESC);

-- Partial index: only unacknowledged alarms, ordered by detection time
-- descending. Powers `memory_list_drift_alarms(acknowledged=False)` and
-- the operator dashboard "needs attention" view.
CREATE INDEX IF NOT EXISTS idx_drift_alarms_unack
    ON drift_alarms(detected_at DESC) WHERE acknowledged_at IS NULL;
