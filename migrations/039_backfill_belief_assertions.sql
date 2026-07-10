-- 039: backfill belief_assertions for every kg_facts row lacking one.
INSERT INTO belief_assertions
    (fact_id, memory_id, belief_status, confidence, epistemic_source,
     certainty_tier, rationale, review_count, created_at, updated_at)
SELECT kf.id, kf.source_memory, kf.belief_status, kf.confidence,
       kf.epistemic_source, 'likely', 'backfilled_v039', 0,
       kf.first_seen, kf.last_seen
FROM kg_facts kf
LEFT JOIN belief_assertions ba ON ba.fact_id = kf.id
WHERE ba.id IS NULL;