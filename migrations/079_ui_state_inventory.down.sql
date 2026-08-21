-- 079 down: drop the UI-state inventory view.
DROP TABLE IF EXISTS ui_state_inventory_fts;
DROP INDEX IF EXISTS idx_uisi_source;
DROP INDEX IF EXISTS idx_uisi_traj_step;
DROP TABLE IF EXISTS ui_state_inventory;
