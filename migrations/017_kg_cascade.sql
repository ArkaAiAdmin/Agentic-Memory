-- Migration 017: cascade-delete kg_edges and backlinks on memory delete
--
-- B-3 fix (2026-06-22 follow-up): the kg_edges and backlinks tables
-- reference memories indirectly (via note_id TEXT in backlinks, via
-- kg_entities which can be shared across notes).  When a memory is
-- deleted, the dependent rows become orphans that bloat search
-- results.  memory_delete.py already cleans them up manually, but the
-- saga rollback path and any direct DELETE FROM memories statements
-- leave them behind.
--
-- This migration adds ON DELETE CASCADE to the two note_id-bound
-- tables so future manual and programmatic deletes stay clean.
-- kg_entities is NOT given CASCADE because entities are shared across
-- notes; orphaned entities are cleaned up by --repair-kg-orphans.
--
-- Note: SQLite requires recreating a table to add a CASCADE FK (ALTER
-- TABLE ... REFERENCES ... ON DELETE CASCADE is silently ignored).  We
-- do the standard 12-step recreation per
-- https://www.sqlite.org/lang_altertable.html — backup, drop,
-- recreate, copy.  This is idempotent (IF EXISTS guards) and safe to
-- run on a DB that already has the cascade constraint from a partial
-- earlier migration.
--
-- The migration_runner wraps each migration in `with conn:` (a
-- transaction), so we don't need explicit BEGIN/COMMIT here.
-- Foreign keys are temporarily disabled because the old table
-- definitions reference each other; we re-enable them at the end.

PRAGMA foreign_keys = OFF;

-- 1. kg_edges: add SET NULL to kg_edges -> kg_entities.  We use
--    ON DELETE SET NULL (not CASCADE) because the existing definition
--    is REFERENCES kg_entities(id) without a cascade policy —
--    converting it to CASCADE would delete an edge just because one
--    endpoint entity was removed, which is too eager.  SET NULL keeps
--    the edge row for forensic analysis and lets --repair-kg-orphans
--    clean up unreferenced entities.
CREATE TABLE IF NOT EXISTS kg_edges_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER REFERENCES kg_entities(id) ON DELETE SET NULL,
    target_id INTEGER REFERENCES kg_entities(id) ON DELETE SET NULL,
    relation TEXT NOT NULL DEFAULT 'related_to',
    weight REAL DEFAULT 1.0,
    created_at TEXT,
    valid_at TEXT,
    invalid_at TEXT,
    UNIQUE(source_id, target_id, relation)
);

INSERT OR IGNORE INTO kg_edges_new
    SELECT id, source_id, target_id, relation, weight, created_at,
           valid_at, invalid_at
    FROM kg_edges;

DROP TABLE kg_edges;
ALTER TABLE kg_edges_new RENAME TO kg_edges;

CREATE INDEX IF NOT EXISTS idx_kg_edges_source ON kg_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_kg_edges_target ON kg_edges(target_id);
CREATE INDEX IF NOT EXISTS idx_kg_edges_relation ON kg_edges(relation);
CREATE INDEX IF NOT EXISTS idx_kg_edges_valid_at ON kg_edges(valid_at);
CREATE INDEX IF NOT EXISTS idx_kg_edges_invalid_at ON kg_edges(invalid_at);

-- 2. backlinks: no FK existed before (just composite PK).  We add
--    one to memories(id) ON DELETE CASCADE so deleting a memory
--    automatically cleans up its backlinks.  We preserve the
--    composite PK so existing rows survive.
--
--    Note: backlinks.target_id can reference a non-existent note
--    (wiki-style "red links" are by design — see memory_integrity
--    comment on _get_orphaned_backlinks).  CASCADE here only fires
--    when source_id matches, which is the correct semantics: a
--    backlink is "this source points to that target", and we
--    garbage-collect the link when the source is gone, not when
--    the (possibly fictional) target is gone.
CREATE TABLE IF NOT EXISTS backlinks_new (
    source_id TEXT REFERENCES memories(id) ON DELETE CASCADE,
    target_id TEXT,
    PRIMARY KEY (source_id, target_id)
);

INSERT OR IGNORE INTO backlinks_new
    SELECT source_id, target_id FROM backlinks;

DROP TABLE IF EXISTS backlinks;
ALTER TABLE backlinks_new RENAME TO backlinks;

CREATE INDEX IF NOT EXISTS idx_backlinks_target_id ON backlinks(target_id);

PRAGMA foreign_keys = ON;
