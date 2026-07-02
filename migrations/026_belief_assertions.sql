-- Migration 026: belief_assertions table — fact/belief separation
--
-- The belief_assertions table stores the agent's model of its own
-- knowledge state, distinguishing "what is true" (kg_facts) from
-- "what I believe" (belief_assertions) and the evidence that connects them.
--
-- Every kg_facts row MAY have a corresponding belief_assertions row.
-- The belief layer is additive — does not change kg_facts semantics.
--
-- Also adds fact_type column to kg_facts for belief type taxonomy:
--   observation | agent_inference | external_stated | hypothesis | derived

-- belief_assertions table
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

-- fact_type column on kg_facts for belief type taxonomy
ALTER TABLE kg_facts ADD COLUMN fact_type TEXT DEFAULT 'observation';

-- Index for fact_type filtering
CREATE INDEX IF NOT EXISTS idx_kg_facts_fact_type ON kg_facts(fact_type);
