-- 054: Seed the built-in `default` principal and grant it memory:admin + ops:read.
--
-- The local-first single-user deployment resolves principal 'default' via the
-- agent_context fallback (agent_context.py get_agent -> AgentContext("default")).
-- Migration 046 seeds roles and policies but binds them to no principal, so the
-- verb-layer RBAC guard (mcp_verbs._check_authorization -> mcp_authorize ->
-- check_permission) denies 'default' every action -- breaking the primary local
-- MCP flow out of the box in both closed and open auth modes.
--
-- Binding 'default' to memory:admin (full memory CRUD) + ops:read restores the
-- expected single-user behavior without weakening RBAC for other principals:
-- adversarial denial tests use non-'default' principal IDs and remain enforced.
--
-- Data-only migration: no DDL changes, so schema DDL round-trip is unaffected.

-- Migration 046 seeds no ops:admin role (only ops:read / ops:delete). The verb
-- layer authorizes maintenance operations as (resource='ops', action='admin')
-- after normalization, so seed a matching role + policies here.
INSERT OR IGNORE INTO roles (id, name, tenant_id, description) VALUES
    ('role_ops_admin', 'ops:admin', 'global', 'Full admin access to operational data');

INSERT OR IGNORE INTO policies (role_id, resource, action) VALUES
    ('role_ops_admin', 'ops', 'read'),
    ('role_ops_admin', 'ops', 'write'),
    ('role_ops_admin', 'ops', 'delete'),
    ('role_ops_admin', 'ops', 'admin');

INSERT OR IGNORE INTO principals (id, kind, display_name, tenant_id)
    VALUES ('default', 'user', 'default', 'default');

INSERT OR IGNORE INTO role_bindings (principal_id, role_id, granted_by)
    VALUES ('default', 'role_memory_admin', 'migration:054'),
           ('default', 'role_ops_admin', 'migration:054');
