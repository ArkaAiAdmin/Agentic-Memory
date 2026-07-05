-- Migration 031: Outbox events for REST & WebSocket streaming APIs
-- Adds memory_events table and triggers on memories table.

CREATE TABLE IF NOT EXISTS memory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,       -- 'memory_added', 'memory_updated', 'memory_deleted'
    note_id TEXT NOT NULL,
    payload TEXT NOT NULL,          -- JSON payload representing the event data
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Trigger for INSERT (memory added)
CREATE TRIGGER IF NOT EXISTS after_insert_memories_events
AFTER INSERT ON memories
FOR EACH ROW
BEGIN
    INSERT INTO memory_events (event_type, note_id, payload)
    VALUES (
        'memory_added',
        NEW.id,
        json_object(
            'id', NEW.id,
            'content', NEW.content,
            'tags', NEW.tags,
            'category', NEW.category,
            'created_at', NEW.created_at
        )
    );
END;

-- Trigger for UPDATE (memory updated, soft-deleted, or restored)
CREATE TRIGGER IF NOT EXISTS after_update_memories_events
AFTER UPDATE ON memories
FOR EACH ROW
BEGIN
    INSERT INTO memory_events (event_type, note_id, payload)
    VALUES (
        IIF(
            OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL,
            'memory_deleted',
            IIF(
                OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS NULL,
                'memory_added',
                'memory_updated'
            )
        ),
        NEW.id,
        json_object(
            'id', NEW.id,
            'content', NEW.content,
            'tags', NEW.tags,
            'category', NEW.category,
            'created_at', NEW.created_at,
            'updated_at', NEW.updated_at,
            'deleted_at', NEW.deleted_at
        )
    );
END;

-- Trigger for DELETE (hard-deleted)
CREATE TRIGGER IF NOT EXISTS after_delete_memories_events
AFTER DELETE ON memories
FOR EACH ROW
BEGIN
    INSERT INTO memory_events (event_type, note_id, payload)
    VALUES (
        'memory_deleted',
        OLD.id,
        json_object(
            'id', OLD.id,
            'content', OLD.content,
            'tags', OLD.tags,
            'category', OLD.category,
            'created_at', OLD.created_at
        )
    );
END;
