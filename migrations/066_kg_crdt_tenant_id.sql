-- Migration 066: Align KG CRDT op-log tables with the sync-server contract
-- Sprint 3 / multi-tenant isolation.
--
-- The live kg_entity_crdt / kg_edge_crdt tables (created by migration
-- 021, later given tenant_id by migration 055) still lack columns the
-- append-only op-log design relies on:
--   * applied    — projection-state tracking column (declared in the
--                  canonical create-table script since Sprint 2.1).
--   * fingerprint — entity inception fingerprint (paper feature, used by
--                  entity_dedup_via_crdt for name/fingerprint collision
--                  resolution).  Entity-only: edges are keyed by
--                  (source_id, target_id, relation) and have no
--                  fingerprint, per paper_pipeline/DESIGN_inception_fingerprint.md.
--
-- tenant_id is intentionally NOT touched here: it is already added by
-- migration 055, and re-adding it produces a harmless-but-noisy
-- "duplicate column" warning on fresh DBs.  Keeping 066 scoped to the
-- two genuinely-missing columns makes it idempotent with no false
-- signals.  SCHEMA_VERSION stays 64 (additive only).
--
-- Re-runs where these columns already exist are tolerated by the
-- migration runner ("duplicate column" -> non-fatal).

ALTER TABLE kg_entity_crdt ADD COLUMN applied INTEGER DEFAULT 0;
ALTER TABLE kg_edge_crdt ADD COLUMN applied INTEGER DEFAULT 0;
ALTER TABLE kg_entity_crdt ADD COLUMN fingerprint TEXT;
