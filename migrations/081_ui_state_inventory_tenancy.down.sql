-- 081 down: Revert ui_state_inventory to pre-tenancy schema.
DROP INDEX IF EXISTS idx_uisi_tenant_traj_step;

CREATE TABLE ui_state_inventory_old (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    traj_id       TEXT NOT NULL,
    step          INTEGER NOT NULL,
    vals          TEXT NOT NULL,
    ctx           TEXT NOT NULL DEFAULT '',
    source_memory TEXT NOT NULL,
    built_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO ui_state_inventory_old (id, traj_id, step, vals, ctx, source_memory, built_at)
SELECT id, traj_id, step, vals, ctx, source_memory, built_at
FROM ui_state_inventory;

DROP TABLE ui_state_inventory;
ALTER TABLE ui_state_inventory_old RENAME TO ui_state_inventory;

CREATE INDEX IF NOT EXISTS idx_uisi_traj_step ON ui_state_inventory(traj_id, step);
CREATE UNIQUE INDEX IF NOT EXISTS idx_uisi_source ON ui_state_inventory(source_memory);
