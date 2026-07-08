from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection

logger = logging.getLogger(__name__)

_KG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kg_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    entity_type TEXT,
    mentions INTEGER DEFAULT 1,
    centrality REAL DEFAULT 0.0,
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(name, entity_type)
);

CREATE INDEX IF NOT EXISTS idx_kg_entities_name ON kg_entities(name);
CREATE INDEX IF NOT EXISTS idx_kg_entities_type ON kg_entities(entity_type);

CREATE TABLE IF NOT EXISTS kg_entity_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    UNIQUE(entity_id, alias)
);

CREATE INDEX IF NOT EXISTS idx_kg_entity_aliases_alias ON kg_entity_aliases(alias);

CREATE TABLE IF NOT EXISTS kg_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES kg_entities(id),
    target_id INTEGER NOT NULL REFERENCES kg_entities(id),
    relation TEXT NOT NULL DEFAULT 'related_to',
    weight REAL DEFAULT 1.0,
    created_at TEXT,
    valid_at TEXT,
    invalid_at TEXT,
    UNIQUE(source_id, target_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_kg_edges_source ON kg_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_kg_edges_target ON kg_edges(target_id);
CREATE INDEX IF NOT EXISTS idx_kg_edges_relation ON kg_edges(relation);
CREATE INDEX IF NOT EXISTS idx_kg_edges_valid_at ON kg_edges(valid_at);
CREATE INDEX IF NOT EXISTS idx_kg_edges_invalid_at ON kg_edges(invalid_at);
"""

_KG_FTS5_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS kg_entities_fts USING fts5(
    name, entity_type, content='kg_entities', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS kg_entities_fts_ai AFTER INSERT ON kg_entities BEGIN
    INSERT INTO kg_entities_fts(rowid, name, entity_type)
        VALUES (new.id, new.name, new.entity_type);
END;

CREATE TRIGGER IF NOT EXISTS kg_entities_fts_ad AFTER DELETE ON kg_entities BEGIN
    INSERT INTO kg_entities_fts(kg_entities_fts, rowid, name, entity_type)
        VALUES('delete', old.id, old.name, old.entity_type);
END;

CREATE TRIGGER IF NOT EXISTS kg_entities_fts_au AFTER UPDATE ON kg_entities BEGIN
    INSERT INTO kg_entities_fts(kg_entities_fts, rowid, name, entity_type)
        VALUES('delete', old.id, old.name, old.entity_type);
    INSERT INTO kg_entities_fts(rowid, name, entity_type)
        VALUES (new.id, new.name, new.entity_type);
END;
"""


def ensure_kg_schema(conn: AnyConnection) -> None:
    """Create KG tables if they don't exist. Also add temporal columns to existing tables."""
    conn.executescript(_KG_SCHEMA_SQL)
    # Migrate existing tables: add valid_at/invalid_at/centrality if missing
    try:
        cols_edges = {
            row[1] for row in conn.execute("PRAGMA table_info(kg_edges)").fetchall()
        }
        if "valid_at" not in cols_edges:
            conn.execute("ALTER TABLE kg_edges ADD COLUMN valid_at TEXT")
        if "invalid_at" not in cols_edges:
            conn.execute("ALTER TABLE kg_edges ADD COLUMN invalid_at TEXT")
        
        cols_entities = {
            row[1] for row in conn.execute("PRAGMA table_info(kg_entities)").fetchall()
        }
        if "centrality" not in cols_entities:
            conn.execute("ALTER TABLE kg_entities ADD COLUMN centrality REAL DEFAULT 0.0")
        if "community_id" not in cols_entities:
            try:
                conn.execute("ALTER TABLE kg_entities ADD COLUMN community_id INTEGER DEFAULT 0")
            except Exception as e:
                logger.warning("ensure_kg_schema failed: %s", e)
        if "betweenness" not in cols_entities:
            try:
                conn.execute("ALTER TABLE kg_entities ADD COLUMN betweenness REAL DEFAULT 0.0")
            except Exception as e:
                logger.warning("ensure_kg_schema failed: %s", e)
    except Exception as exc:
        logger.debug("KG schema migration (ALTER TABLE) skipped: %s", exc)

    # FTS5 entity search index
    try:
        conn.executescript(_KG_FTS5_DDL)
    except Exception as exc:
        logger.debug("KG FTS5 DDL skipped: %s", exc)

    # Backfill FTS if table exists but FTS is empty (first migration)
    try:
        fts_count_row = conn.execute("SELECT COUNT(*) FROM kg_entities_fts").fetchone()
        fts_count = int(fts_count_row[0]) if fts_count_row is not None else 0
        entity_count_row = conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()
        entity_count = int(entity_count_row[0]) if entity_count_row is not None else 0
        if fts_count == 0 and entity_count > 0:
            conn.execute(
                "INSERT INTO kg_entities_fts(rowid, name, entity_type) "
                "SELECT id, name, entity_type FROM kg_entities"
            )
    except Exception:
        logger.warning("FTS5 table doesn't exist, skipping KG entities FTS backfill")
        pass

    # Extraction stats (P2a.2). The migration runner also creates this
    # table, but we create it defensively here so a fresh DB that has
    # not run the migration yet still gets the table when the KG is
    # enabled via ensure_kg_schema().
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kg_extraction_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id TEXT NOT NULL,
                entities_extracted INTEGER DEFAULT 0,
                regex_count INTEGER DEFAULT 0,
                llm_count INTEGER DEFAULT 0,
                duration_ms REAL DEFAULT 0,
                error TEXT,
                created_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_kg_extraction_stats_memory
                ON kg_extraction_stats(memory_id);
            CREATE INDEX IF NOT EXISTS idx_kg_extraction_stats_created
                ON kg_extraction_stats(created_at);
            """
        )
    except Exception as exc:
        logger.debug("kg_extraction_stats table creation skipped: %s", exc)
