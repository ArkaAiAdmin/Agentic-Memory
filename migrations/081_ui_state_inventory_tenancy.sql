-- 081: Add tenant scoping to ui_state_inventory.
ALTER TABLE ui_state_inventory ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_uisi_tenant_traj_step ON ui_state_inventory(tenant_id, traj_id, step);
