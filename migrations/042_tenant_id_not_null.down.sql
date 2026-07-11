-- 042 down: Restore tenant_id as nullable on memories table.
--
-- SQLite doesn't support ALTER COLUMN, so recreate the table.
-- FTS triggers are recreated by infra/fts.py at schema setup time.

-- Step 1: Create old-style table with nullable tenant_id
CREATE TABLE memories_old (
    id                TEXT PRIMARY KEY,
    content           TEXT    NOT NULL,
    source_file       TEXT    NOT NULL,
    tags              TEXT    DEFAULT '[]',
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    observed_at       TEXT    NOT NULL,
    pinned            INTEGER DEFAULT 0,
    importance        INTEGER DEFAULT 3,
    decay             TEXT    DEFAULT 'none',
    score             REAL    DEFAULT 1.0,
    supersedes        TEXT,
    repo_id           TEXT,
    access_count      INTEGER DEFAULT 1,
    success_score     REAL    DEFAULT 0.0,
    fitness_score     REAL    DEFAULT 1.0,
    conflict_policy   TEXT    DEFAULT 'supersede',
    version_vector    TEXT    DEFAULT '{}',
    logical_clock     INTEGER DEFAULT 0,
    consolidation_state TEXT DEFAULT 'working',
    tenant_id         TEXT    DEFAULT 'default',
    valid_from        TEXT,
    valid_to          TEXT,
    superseded_by     TEXT,
    last_accessed     TEXT,
    deleted_at        TEXT,
    deleted_by        TEXT,
    context_prefix    TEXT,
    category          TEXT,
    tier              TEXT,
    importance_score  REAL,
    metadata          TEXT
);

-- Step 2: Copy all data
INSERT INTO memories_old (
    id, content, source_file, tags, created_at, updated_at, observed_at,
    pinned, importance, decay, score, supersedes, repo_id, access_count,
    success_score, fitness_score, conflict_policy, version_vector,
    logical_clock, consolidation_state, tenant_id, valid_from, valid_to,
    superseded_by, last_accessed, deleted_at, deleted_by, context_prefix,
    category, tier, importance_score, metadata
)
SELECT
    id, content, source_file, tags, created_at, updated_at, observed_at,
    pinned, importance, decay, score, supersedes, repo_id, access_count,
    success_score, fitness_score, conflict_policy, version_vector,
    logical_clock, consolidation_state, tenant_id, valid_from, valid_to,
    superseded_by, last_accessed, deleted_at, deleted_by, context_prefix,
    category, tier, importance_score, metadata
FROM memories;

-- Step 3: Drop current table and rename old
DROP TABLE memories;
ALTER TABLE memories_old RENAME TO memories;

-- Step 4: Recreate all indexes (without tenant_id index)
CREATE INDEX idx_memories_repo_id ON memories(repo_id);
CREATE INDEX idx_memories_pinned ON memories(pinned);
CREATE INDEX idx_memories_consolidation_state ON memories(consolidation_state);
CREATE INDEX idx_memories_created_at ON memories(created_at);
CREATE INDEX idx_memories_updated_at ON memories(updated_at);
CREATE INDEX idx_memories_observed_at ON memories(observed_at);
CREATE INDEX idx_memories_fitness_score ON memories(fitness_score);
CREATE INDEX idx_memories_source_file ON memories(source_file);
CREATE INDEX idx_memories_valid_to ON memories(valid_to);
CREATE INDEX idx_memories_valid_from ON memories(valid_from);
CREATE INDEX idx_memories_superseded_by ON memories(superseded_by);
CREATE INDEX idx_memories_last_accessed ON memories(last_accessed);
CREATE INDEX idx_memories_deleted_at ON memories(deleted_at);

-- Step 5: Recreate outbox triggers (dropped with old table)
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

CREATE TRIGGER IF NOT EXISTS after_update_memories_events
AFTER UPDATE OF content, tags, category, deleted_at ON memories
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
