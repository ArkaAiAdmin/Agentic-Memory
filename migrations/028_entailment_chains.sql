CREATE TABLE IF NOT EXISTS entailment_chains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_fact_ids TEXT NOT NULL,
    derived_fact_id INTEGER REFERENCES kg_facts(id),
    derivation_type TEXT NOT NULL,
    confidence REAL NOT NULL,
    derived_at REAL NOT NULL,
    last_validated_at REAL,
    valid BOOLEAN DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_entailment_chains_derived_fact
    ON entailment_chains(derived_fact_id);
CREATE INDEX IF NOT EXISTS idx_entailment_chains_type
    ON entailment_chains(derivation_type, valid, derived_at);
