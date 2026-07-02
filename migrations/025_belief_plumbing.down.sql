-- Down migration for 025: remove belief-layer columns from kg_facts

DROP INDEX IF EXISTS idx_kg_facts_epistemic_source;
DROP INDEX IF EXISTS idx_kg_facts_belief_status;

-- SQLite >= 3.35 supports DROP COLUMN. These columns were added in 025
-- and carry no NOT NULL / primary-key / unique constraints, so safe to drop.
ALTER TABLE kg_facts DROP COLUMN embedding;
ALTER TABLE kg_facts DROP COLUMN evidence_chain;
ALTER TABLE kg_facts DROP COLUMN asserting_agent_id;
ALTER TABLE kg_facts DROP COLUMN epistemic_source;
ALTER TABLE kg_facts DROP COLUMN belief_status;
