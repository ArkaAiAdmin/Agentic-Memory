DROP INDEX IF EXISTS idx_kg_facts_entailed;
CREATE TABLE kg_facts_new AS
  SELECT id, subject, predicate, object, confidence, locked,
         first_seen, last_seen, mention_count,
         subject_entity_id, object_entity_id,
         source_memory, belief_status, epistemic_source, fact_type,
         invalid_at, superseded_by, supersedes, invalidation_reason,
         event_time, event_time_granularity, transaction_time, valid_at,
         contradiction_score, context, asserting_agent_id, evidence_chain,
         embedding
  FROM kg_facts;
DROP TABLE kg_facts;
ALTER TABLE kg_facts_new RENAME TO kg_facts;
