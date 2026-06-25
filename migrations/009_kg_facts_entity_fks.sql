-- Migration 009: add entity_id FKs to kg_facts
--
-- kg_facts was denormalized (subject/object as free TEXT). This migration
-- adds nullable entity_id columns and FKs to kg_entities so future rows
-- can be linked back. Existing rows are left with NULL entity_ids; a
-- separate backfill can populate them.
--
-- B24 fix.

ALTER TABLE kg_facts ADD COLUMN subject_entity_id INTEGER REFERENCES kg_entities(id);
ALTER TABLE kg_facts ADD COLUMN object_entity_id INTEGER REFERENCES kg_entities(id);

CREATE INDEX IF NOT EXISTS idx_kg_facts_subject_entity ON kg_facts(subject_entity_id);
CREATE INDEX IF NOT EXISTS idx_kg_facts_object_entity ON kg_facts(object_entity_id);
