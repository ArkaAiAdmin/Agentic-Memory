-- Rollback Migration 032: restore unscoped update trigger

DROP TRIGGER IF EXISTS after_update_memories_events;

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
