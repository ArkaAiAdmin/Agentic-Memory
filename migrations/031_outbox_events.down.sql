-- Rollback Migration 031: Outbox events

DROP TRIGGER IF EXISTS after_delete_memories_events;
DROP TRIGGER IF EXISTS after_update_memories_events;
DROP TRIGGER IF EXISTS after_insert_memories_events;
DROP TABLE IF EXISTS memory_events;
