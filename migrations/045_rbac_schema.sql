-- 045: RBAC schema — roles, role_bindings, policies, acl_overrides, audit.

CREATE TABLE IF NOT EXISTS roles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(name, tenant_id)
);

CREATE INDEX IF NOT EXISTS idx_roles_tenant ON roles(tenant_id);

CREATE TABLE IF NOT EXISTS role_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    granted_at TEXT NOT NULL DEFAULT (datetime('now')),
    granted_by TEXT,
    UNIQUE(principal_id, role_id)
);

CREATE INDEX IF NOT EXISTS idx_role_bindings_principal ON role_bindings(principal_id);
CREATE INDEX IF NOT EXISTS idx_role_bindings_role ON role_bindings(role_id);

CREATE TABLE IF NOT EXISTS policies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    resource TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('read','write','delete','admin','export')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(role_id, resource, action)
);

CREATE INDEX IF NOT EXISTS idx_policies_role ON policies(role_id);
CREATE INDEX IF NOT EXISTS idx_policies_resource ON policies(resource);

CREATE TABLE IF NOT EXISTS acl_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    principal_id TEXT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    resource_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('read','write','delete','admin','export')),
    effect TEXT NOT NULL DEFAULT 'deny' CHECK(effect IN ('allow','deny')),
    granted_at TEXT NOT NULL DEFAULT (datetime('now')),
    granted_by TEXT,
    UNIQUE(principal_id, resource_id, action)
);

CREATE INDEX IF NOT EXISTS idx_acl_overrides_principal ON acl_overrides(principal_id);

CREATE TABLE IF NOT EXISTS principal_roles_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    principal_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('grant','revoke')),
    performed_by TEXT,
    performed_at TEXT NOT NULL DEFAULT (datetime('now')),
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_principal_roles_audit_principal ON principal_roles_audit(principal_id);
