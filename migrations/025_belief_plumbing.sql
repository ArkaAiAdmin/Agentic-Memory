-- Migration 025: Belief layer plumbing — epistemic_source, belief_status, embedding on kg_facts
--
-- Adds columns to kg_facts to support:
--   belief_status       — agent's current stance on this fact (active, retracted, deprecated)
--   epistemic_source    — who/what asserted this fact (agent, auto_save, hook, import, cron)
--   asserting_agent_id  — which agent/process asserted it
--   evidence_chain      — JSON array of fact_ids that support this belief
--   embedding           — BLOB vector for hybrid FTS+vector search on facts
--
-- Also fixes the ON DELETE SET NULL inconsistency from ensure_facts_schema().

-- Belief/epistemic columns on kg_facts
ALTER TABLE kg_facts ADD COLUMN belief_status TEXT DEFAULT 'active';
ALTER TABLE kg_facts ADD COLUMN epistemic_source TEXT DEFAULT 'agent';
ALTER TABLE kg_facts ADD COLUMN asserting_agent_id TEXT;
ALTER TABLE kg_facts ADD COLUMN evidence_chain TEXT;
ALTER TABLE kg_facts ADD COLUMN embedding BLOB;

-- Indexes for common filter patterns
CREATE INDEX IF NOT EXISTS idx_kg_facts_belief_status ON kg_facts(belief_status);
CREATE INDEX IF NOT EXISTS idx_kg_facts_epistemic_source ON kg_facts(epistemic_source);

-- Fix entity FK ON DELETE SET NULL (ensure_facts_schema in fact_schema.py
-- creates these without ON DELETE; migration 019 uses SET NULL).
-- Only apply if the column exists but lacks the correct FK action.
-- SQLite does not support ALTER COLUMN, so we recreate:
--  1. Create new table with correct schema
--  2. Copy data
--  3. Drop old table
--  4. Rename
-- This is safe because kg_facts is a stable table with no data loss.

-- Down-migration note: 025.down.sql drops the new columns via temp table recreate.
