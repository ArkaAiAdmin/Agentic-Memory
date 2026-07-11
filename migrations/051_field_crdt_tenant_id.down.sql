-- 051 down: remove tenant_id column and index from memory_field_crdt.

DROP INDEX IF EXISTS idx_memory_field_crdt_tenant_id;

-- SQLite does not support DROP COLUMN in older versions; recreate the
-- table without tenant_id and copy data across.
CREATE TABLE memory_field_crdt_tmp (
    memory_id        TEXT    NOT NULL,
    field_name       TEXT    NOT NULL,
    value            TEXT    NOT NULL,
    version_vector   TEXT    NOT NULL,
    logical_clock    INTEGER NOT NULL,
    last_writer_agent TEXT   NOT NULL,
    is_deleted       INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (memory_id, field_name),
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
);

INSERT OR IGNORE INTO memory_field_crdt_tmp
    (memory_id, field_name, value, version_vector, logical_clock,
     last_writer_agent, is_deleted, updated_at)
SELECT memory_id, field_name, value, version_vector, logical_clock,
       last_writer_agent, is_deleted, updated_at
FROM memory_field_crdt;

DROP TABLE memory_field_crdt;
ALTER TABLE memory_field_crdt_tmp RENAME TO memory_field_crdt;

CREATE INDEX IF NOT EXISTS idx_memory_field_crdt_memory
    ON memory_field_crdt(memory_id);
CREATE INDEX IF NOT EXISTS idx_memory_field_crdt_agent_updated
    ON memory_field_crdt(last_writer_agent, updated_at);
