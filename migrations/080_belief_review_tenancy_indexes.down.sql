-- 080 down: drop belief review tenancy indexes.
DROP INDEX IF EXISTS idx_review_queue_tenant_status;
DROP INDEX IF EXISTS idx_belief_assertions_tenant_review;
DROP INDEX IF EXISTS idx_belief_review_queue_pending_unique;
