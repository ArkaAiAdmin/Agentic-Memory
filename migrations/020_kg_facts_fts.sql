-- Migration 020: kg_facts FTS5 index + sync triggers
--
-- 2026-06-23 (asymmetry fix): kg_facts is the only text-searchable
-- table without an FTS5 virtual table.  The other three
-- (memories, memory_chunks, kg_entities) all have:
--   * an FTS5 virtual table (content='<table>', content_rowid='id')
--   * 3 sync triggers: after insert (ai), after delete (ad), after update (au)
--
-- Without FTS, facts_search() in fact_extraction.py uses
--   SELECT ... FROM kg_facts WHERE subject LIKE ? OR predicate LIKE ? OR object LIKE ?
-- which is O(n) on the table (no index can help with leading
-- wildcard).  The memory_facts_search admin MCP tool is slow on
-- large KBs.
--
-- This migration:
--   1. Creates kg_facts_fts FTS5 virtual table (contentless, indexed
--      on subject/predicate/object/context) backed by kg_facts
--   2. Creates 3 sync triggers (ai, ad, au) that keep the FTS table
--      in lockstep with kg_facts
--   3. Backfills existing kg_facts rows into the new FTS table
--
-- After this migration, future fact search can use FTS5 ranked search
-- instead of LIKE.  The existing facts_search() function is left
-- unchanged for backward compat — callers can opt into the FTS path
-- separately.  The triggers ensure the FTS index is always current,
-- so any new FTS-using code can rely on consistency with kg_facts.
--
-- Cost: ~3 trigger invocations per fact INSERT/UPDATE/DELETE.  Each
-- invocation is a single-row FTS insert/delete (microseconds).  The
-- backfill is a single bulk INSERT (~2000 rows, milliseconds).

-- ---------------------------------------------------------------------------
-- 1. Create the FTS5 virtual table
-- ---------------------------------------------------------------------------

CREATE VIRTUAL TABLE IF NOT EXISTS kg_facts_fts USING fts5(
    subject, predicate, object, context,
    content='kg_facts', content_rowid='id',
    tokenize='porter unicode61'
);

-- ---------------------------------------------------------------------------
-- 2. Create the sync triggers
-- ---------------------------------------------------------------------------

CREATE TRIGGER IF NOT EXISTS kg_facts_fts_ai AFTER INSERT ON kg_facts BEGIN
    INSERT INTO kg_facts_fts(rowid, subject, predicate, object, context)
    VALUES (new.id, new.subject, new.predicate, new.object, new.context);
END;

CREATE TRIGGER IF NOT EXISTS kg_facts_fts_ad AFTER DELETE ON kg_facts BEGIN
    INSERT INTO kg_facts_fts(kg_facts_fts, rowid, subject, predicate, object, context)
    VALUES('delete', old.id, old.subject, old.predicate, old.object, old.context);
END;

CREATE TRIGGER IF NOT EXISTS kg_facts_fts_au AFTER UPDATE ON kg_facts BEGIN
    INSERT INTO kg_facts_fts(kg_facts_fts, rowid, subject, predicate, object, context)
    VALUES('delete', old.id, old.subject, old.predicate, old.object, old.context);
    INSERT INTO kg_facts_fts(rowid, subject, predicate, object, context)
    VALUES (new.id, new.subject, new.predicate, new.object, new.context);
END;

-- ---------------------------------------------------------------------------
-- 3. Backfill existing facts into the FTS table
-- ---------------------------------------------------------------------------

-- Use the FTS5 'rebuild' command: this re-indexes the entire external
-- content table (kg_facts) into the FTS shadow tables.  This is the
-- canonical way to backfill a contentless FTS5 table; a plain
-- INSERT INTO kg_facts_fts(rowid, subject, ...) does not properly
-- populate the FTS index for contentless tables (verified: only the
-- rows inserted via the 'rebuild' command are queryable via MATCH).
INSERT INTO kg_facts_fts(kg_facts_fts) VALUES('rebuild');
