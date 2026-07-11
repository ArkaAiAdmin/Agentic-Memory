-- 056: Add tenant_id to memory_skills for tenant isolation.
--
-- memory_skills stores procedural knowledge (extracted skills)
-- and is queried by the sync server for peer-to-peer skill sync.
-- Without a native tenant_id column, cross-tenant skill isolation
-- relies on application-level filtering.
--
-- This migration adds tenant_id with a DEFAULT of 'default' for
-- backward compatibility. Follows the pattern from migration 050.

ALTER TABLE memory_skills ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_memory_skills_tenant ON memory_skills(tenant_id);
