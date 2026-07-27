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
    subject_entity_id INTEGER REFERENCES kg_entities(id) ON DELETE SET NULL,
    object_entity_id INTEGER REFERENCES kg_entities(id) ON DELETE SET NULL,
    event_time REAL,
    event_time_granularity TEXT,
    transaction_time REAL,
    valid_at REAL,
    invalid_at REAL,
    superseded_by INTEGER REFERENCES kg_facts(id) ON DELETE SET NULL,
    supersedes INTEGER REFERENCES kg_facts(id) ON DELETE SET NULL,
    contradiction_score REAL DEFAULT 0.0,
    invalidation_reason TEXT,
    belief_status TEXT DEFAULT 'active',
    epistemic_source TEXT DEFAULT 'agent',
    asserting_agent_id TEXT,
    evidence_chain TEXT,
    embedding BLOB,
    fact_type TEXT DEFAULT 'observation',
    is_entailed BOOLEAN DEFAULT 0,
    tenant_id TEXT NOT NULL DEFAULT 'default',
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

    The base schema includes all columns from the migration chain
    (009, 018, 019, 025, 026, 034, 050) so fresh DBs are fully
    functional without running the migration chain. Existing migrated
    DBs are unaffected — migrations hit ``IF NOT EXISTS`` / see the
    columns already exist and skip them.
    """
    conn.executescript(_FACTS_SCHEMA_SQL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_fact_type ON kg_facts(fact_type)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_belief_status ON kg_facts(belief_status)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_epistemic_source ON kg_facts(epistemic_source)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_subject_entity ON kg_facts(subject_entity_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_object_entity ON kg_facts(object_entity_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_validity ON kg_facts(valid_at, invalid_at)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_superseded_by ON kg_facts(superseded_by)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_event_time ON kg_facts(event_time)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_facts_tenant ON kg_facts(tenant_id)")
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
        "VALUES (new.id, COALESCE(new.subject, ''), COALESCE(new.predicate, ''), "
        "COALESCE(new.object, ''), COALESCE(new.context, '')); "
        "END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS kg_facts_fts_ad AFTER DELETE ON kg_facts BEGIN "
        "INSERT INTO kg_facts_fts(kg_facts_fts, rowid, subject, predicate, object, context) "
        "VALUES('delete', old.id, COALESCE(old.subject, ''), COALESCE(old.predicate, ''), "
        "COALESCE(old.object, ''), COALESCE(old.context, '')); "
        "END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS kg_facts_fts_au AFTER UPDATE ON kg_facts BEGIN "
        "INSERT INTO kg_facts_fts(kg_facts_fts, rowid, subject, predicate, object, context) "
        "VALUES('delete', old.id, COALESCE(old.subject, ''), COALESCE(old.predicate, ''), "
        "COALESCE(old.object, ''), COALESCE(old.context, '')); "
        "INSERT INTO kg_facts_fts(rowid, subject, predicate, object, context) "
        "VALUES (new.id, COALESCE(new.subject, ''), COALESCE(new.predicate, ''), "
        "COALESCE(new.object, ''), COALESCE(new.context, '')); "
        "END"
    )
