"""Belief assertions schema for agentic-memory.

The ``belief_assertions`` table stores the agent's model of its own
knowledge state — distinguishing "what is true" (kg_facts) from
"what I believe" (belief_assertions) and the evidence that connects them.

Every kg_facts row MAY have a corresponding belief_assertions row
(the belief layer is additive — does not change kg_facts semantics).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection

logger = logging.getLogger(__name__)

_BELIEF_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS belief_assertions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id INTEGER REFERENCES kg_facts(id) ON DELETE CASCADE,
    memory_id TEXT REFERENCES memories(id) ON DELETE SET NULL,
    belief_status TEXT NOT NULL DEFAULT 'active',
    confidence REAL DEFAULT 1.0,
    epistemic_source TEXT NOT NULL DEFAULT 'agent',
    asserting_agent_id TEXT,
    evidence_chain TEXT,
    rationale TEXT,
    certainty_tier TEXT DEFAULT 'likely',
    last_reviewed_at REAL,
    review_count INTEGER DEFAULT 0,
    created_at REAL,
    updated_at REAL,
    UNIQUE(fact_id)
);

CREATE INDEX IF NOT EXISTS idx_belief_assertions_status ON belief_assertions(belief_status);
CREATE INDEX IF NOT EXISTS idx_belief_assertions_source ON belief_assertions(epistemic_source);
CREATE INDEX IF NOT EXISTS idx_belief_assertions_certainty ON belief_assertions(certainty_tier);
CREATE INDEX IF NOT EXISTS idx_belief_assertions_confidence ON belief_assertions(confidence);
CREATE INDEX IF NOT EXISTS idx_belief_assertions_agent ON belief_assertions(asserting_agent_id);
CREATE INDEX IF NOT EXISTS idx_belief_assertions_fact ON belief_assertions(fact_id);
"""


def ensure_beliefs_schema(conn: AnyConnection) -> None:
    """Create the ``belief_assertions`` table and indexes if they don't exist.

    Idempotent: safe to call on every connection open.
    """
    conn.executescript(_BELIEF_SCHEMA_SQL)
