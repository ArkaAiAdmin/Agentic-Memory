-- 013_field_level_crdt.sql
-- v13: per-field CRDT state for true concurrent-edit semantics.
--
-- 2026-06-20 audit (9-agent parallel review) identified that
-- crdt_merge.py was "LWW-with-Version-Vector" (whole-note), not a
-- true CRDT. Two agents editing DIFFERENT fields of the same note
-- would see one side's entire note win outright. This migration
-- introduces per-field LWW-Element-Set (LWWES) so concurrent
-- edits to different fields both win.
--
-- Design:
--   * One row per (memory_id, field_name). The set of "live" fields
--     is the set of rows where is_deleted = 0.
--   * Each field has its own version_vector and logical_clock.
--     Causal ordering is per-field, not per-note.
--   * Tombstones (is_deleted = 1) are part of the CRDT. They
--     are eventually consistent: a tombstone from agent A will
--     eventually overwrite a live value from agent B only if A's
--     tombstone is causally after B's write.
--   * The note-level `memories.version_vector` and
--     `memories.logical_clock` are kept for backward compat (the
--     existing note-level LWW code still works). They now represent
--     the *latest* write across all fields, computed from
--     MAX(logical_clock) and the union of all field-level VVs.
--
-- Concurrency invariants (provable):
--   * Commutativity: merge_fields(a, b) == merge_fields(b, a) for
--     the same field, because LWW is deterministic and applied
--     independently per field.
--   * Associativity: merge_fields(merge(a, b), c) == merge(a, merge(b, c)),
--     because LWW tiebreaker is a total order.
--   * Idempotence: merge_fields(a, a) == a.
--   * Convergence: all replicas applying the same set of field
--     updates (in any order) reach the same state.
--
-- Storage cost: ~5x for the field table (one row per field per
-- note). Mitigated by the natural clustering on (memory_id,
-- field_name).

CREATE TABLE IF NOT EXISTS memory_field_crdt (
    memory_id        TEXT    NOT NULL,
    field_name       TEXT    NOT NULL,
    value            TEXT    NOT NULL,
    version_vector   TEXT    NOT NULL,        -- JSON dict[agent_id, clock]
    logical_clock    INTEGER NOT NULL,
    last_writer_agent TEXT   NOT NULL,
    is_deleted       INTEGER NOT NULL DEFAULT 0,  -- 0=live, 1=tombstone
    updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (memory_id, field_name),
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
);

-- Index for "what fields are in this note" queries.
CREATE INDEX IF NOT EXISTS idx_memory_field_crdt_memory
    ON memory_field_crdt(memory_id);

-- Index for "what has agent X written lately" queries (sync protocol).
CREATE INDEX IF NOT EXISTS idx_memory_field_crdt_agent_updated
    ON memory_field_crdt(last_writer_agent, updated_at);
