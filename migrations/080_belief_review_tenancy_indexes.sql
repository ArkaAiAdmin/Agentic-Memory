-- 080: Tenancy and review indexes for belief lifecycle.
-- Forward migration: deduplicate pending review queue entries,
-- then create unique index on pending reviews, plus tenancy-first indexes.

DELETE FROM belief_review_queue 
WHERE status = 'pending'
  AND rowid NOT IN (
      SELECT MIN(rowid) 
      FROM belief_review_queue 
      WHERE status = 'pending'
      GROUP BY tenant_id, belief_id
  );

CREATE UNIQUE INDEX IF NOT EXISTS idx_belief_review_queue_pending_unique 
ON belief_review_queue(tenant_id, belief_id) WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_belief_assertions_tenant_review 
ON belief_assertions (tenant_id, belief_status, confidence, last_reviewed_at);

CREATE INDEX IF NOT EXISTS idx_review_queue_tenant_status 
ON belief_review_queue (tenant_id, status);
