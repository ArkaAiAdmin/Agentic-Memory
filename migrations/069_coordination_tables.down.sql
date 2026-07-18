-- Migration 069 down: Remove coordination tables

DROP TABLE IF EXISTS file_locks;
DROP TABLE IF EXISTS agent_messages;
DROP TABLE IF EXISTS project_state;
DROP TABLE IF EXISTS shared_tasks;
