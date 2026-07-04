"""Fact schema management for agentic-memory.

Creates and migrates the ``kg_facts`` table, indexes, FTS5 virtual table,
and sync triggers.  Idempotent — safe to call on every connection open.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection

logger = logging.getLogger(__name__)

_FACTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kg_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    locked INTEGER DEFAULT 0,
    first_seen REAL,
    last_seen REAL,
    mention_count INTEGER DEFAULT 1,
    source_memory TEXT,
    context TEXT,
    UNIQUE(subject, predicate, object)
);

CREATE INDEX IF NOT EXISTS idx_kg_facts_subject ON kg_facts(subject);
CREATE INDEX IF NOT EXISTS idx_kg_facts_predicate ON kg_facts(predicate);
CREATE INDEX IF NOT EXISTS idx_kg_facts_object ON kg_facts(object);
CREATE INDEX IF NOT EXISTS idx_kg_facts_spo ON kg_facts(subject, predicate, object);
"""


def ensure_facts_schema(conn: AnyConnection) -> None:
    """Create the ``kg_facts`` table and indexes if they don't exist.

    Idempotent: safe to call on every connection open. The CREATE
    statements use ``IF NOT EXISTS`` so re-running on a DB that
    already has the table is a no-op.
    """
    # Base table + indexes (idempotent on all schema versions)
    conn.executescript(_FACTS_SCHEMA_SQL)
    # B24: backfill entity FK columns on pre-migration DBs — must run BEFORE
    # entity_id indexes so columns exist.
    # T1.x (temporal-kg plan): backfill the v18 temporal columns on pre-migration
    # DBs — also must run BEFORE the v18 indexes so columns exist.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(kg_facts)").fetchall()}
    if "subject_entity_id" not in cols:
        conn.execute(
            "ALTER TABLE kg_facts ADD COLUMN subject_entity_id INTEGER REFERENCES kg_entities(id) ON DELETE SET NULL"
        )
    if "object_entity_id" not in cols:
        conn.execute(
            "ALTER TABLE kg_facts ADD COLUMN object_entity_id INTEGER REFERENCES kg_entities(id) ON DELETE SET NULL"
        )
    # T1.x: v18 temporal columns. Each column is independent — if a
    # pre-v18 DB has some but not others, only the missing ones are
    # added. All columns are NULL-able so existing rows are unaffected.
    if "event_time" not in cols:
        conn.execute("ALTER TABLE kg_facts ADD COLUMN event_time REAL")
    if "event_time_granularity" not in cols:
        conn.execute("ALTER TABLE kg_facts ADD COLUMN event_time_granularity TEXT")
    if "transaction_time" not in cols:
        conn.execute("ALTER TABLE kg_facts ADD COLUMN transaction_time REAL")
    if "valid_at" not in cols:
        conn.execute("ALTER TABLE kg_facts ADD COLUMN valid_at REAL")
    if "invalid_at" not in cols:
        conn.execute("ALTER TABLE kg_facts ADD COLUMN invalid_at REAL")
    if "superseded_by" not in cols:
        conn.execute(
            "ALTER TABLE kg_facts ADD COLUMN superseded_by INTEGER "
            "REFERENCES kg_facts(id) ON DELETE SET NULL"
        )
    if "supersedes" not in cols:
        conn.execute(
            "ALTER TABLE kg_facts ADD COLUMN supersedes INTEGER "
            "REFERENCES kg_facts(id) ON DELETE SET NULL"
        )
    if "contradiction_score" not in cols:
        conn.execute(
            "ALTER TABLE kg_facts ADD COLUMN contradiction_score REAL DEFAULT 0.0"
        )
    if "invalidation_reason" not in cols:
        conn.execute("ALTER TABLE kg_facts ADD COLUMN invalidation_reason TEXT")
    # B25+S1: belief-layer columns (Sprint 0 + 1).  These are idempotent
    # via the ``if not in cols`` check — same pattern as the temporal
    # columns above.  Fresh DBs get them here; existing DBs get them
    # via migration 025 + 026.
    if "belief_status" not in cols:
        conn.execute("ALTER TABLE kg_facts ADD COLUMN belief_status TEXT DEFAULT 'active'")
    if "epistemic_source" not in cols:
        conn.execute("ALTER TABLE kg_facts ADD COLUMN epistemic_source TEXT DEFAULT 'agent'")
    if "asserting_agent_id" not in cols:
        conn.execute("ALTER TABLE kg_facts ADD COLUMN asserting_agent_id TEXT")
    if "evidence_chain" not in cols:
        conn.execute("ALTER TABLE kg_facts ADD COLUMN evidence_chain TEXT")
    if "fact_type" not in cols:
        conn.execute("ALTER TABLE kg_facts ADD COLUMN fact_type TEXT DEFAULT 'observation'")
    if "embedding" not in cols:
        conn.execute("ALTER TABLE kg_facts ADD COLUMN embedding BLOB")
    # Ensure belief-layer indexes exist.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_fact_type ON kg_facts(fact_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_belief_status ON kg_facts(belief_status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_epistemic_source ON kg_facts(epistemic_source)"
    )
    # Now entity_id and temporal indexes are safe to create.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_subject_entity ON kg_facts(subject_entity_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_object_entity ON kg_facts(object_entity_id)"
    )
    # T1.x: v18 temporal indexes.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_validity ON kg_facts(valid_at, invalid_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_superseded_by ON kg_facts(superseded_by)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_event_time ON kg_facts(event_time)"
    )
    # T20 (2026-06-23): kg_facts FTS5 index. Brings kg_facts in line with
    # the other 3 text-searchable tables (memories, memory_chunks,
    # kg_entities) which all have FTS5 + 3 sync triggers. The FTS is
    # contentless (backed by kg_facts) so it doesn't duplicate storage.
    # Use IF NOT EXISTS so this is safe to call on every connection
    # open.  Idempotent with migration 020_kg_facts_fts.sql.
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS kg_facts_fts USING fts5("
        "subject, predicate, object, context, "
        "content='kg_facts', content_rowid='id', "
        "tokenize='porter unicode61'"
        ")"
    )
    # 3 sync triggers (ai, ad, au). Use IF NOT EXISTS for idempotency.
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS kg_facts_fts_ai AFTER INSERT ON kg_facts BEGIN "
        "INSERT INTO kg_facts_fts(rowid, subject, predicate, object, context) "
        "VALUES (new.id, new.subject, new.predicate, new.object, new.context); "
        "END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS kg_facts_fts_ad AFTER DELETE ON kg_facts BEGIN "
        "INSERT INTO kg_facts_fts(kg_facts_fts, rowid, subject, predicate, object, context) "
        "VALUES('delete', old.id, old.subject, old.predicate, old.object, old.context); "
        "END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS kg_facts_fts_au AFTER UPDATE ON kg_facts BEGIN "
        "INSERT INTO kg_facts_fts(kg_facts_fts, rowid, subject, predicate, object, context) "
        "VALUES('delete', old.id, old.subject, old.predicate, old.object, old.context); "
        "INSERT INTO kg_facts_fts(rowid, subject, predicate, object, context) "
        "VALUES (new.id, new.subject, new.predicate, new.object, new.context); "
        "END"
    )
