-- Down migration: drop session memory tables in reverse order.
-- Disable FK checks because thread_events -> decision_threads and
-- thread_events -> sessions; both sides have data.

PRAGMA foreign_keys = OFF;

DROP TABLE IF EXISTS thread_events;
DROP TABLE IF EXISTS session_compaction_log;
DROP TABLE IF EXISTS decision_threads;
DROP TABLE IF EXISTS sessions;

PRAGMA foreign_keys = ON;
