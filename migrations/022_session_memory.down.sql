-- Down migration: drop session memory tables in reverse order

DROP TABLE IF EXISTS thread_events;
DROP TABLE IF EXISTS session_compaction_log;
DROP TABLE IF EXISTS decision_threads;
DROP TABLE IF EXISTS sessions;
