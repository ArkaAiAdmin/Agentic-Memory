-- 046: Seed built-in roles with their policies.

INSERT OR IGNORE INTO roles (id, name, tenant_id, description) VALUES
    ('role_memory_read', 'memory:read', 'global', 'Read memories in own tenant'),
    ('role_memory_write', 'memory:write', 'global', 'Write memories in own tenant'),
    ('role_memory_delete', 'memory:delete', 'global', 'Delete memories in own tenant'),
    ('role_memory_admin', 'memory:admin', 'global', 'Full memory admin including cross-tenant'),
    ('role_memory_export', 'memory:export', 'global', 'Export memory data'),
    ('role_ops_read', 'ops:read', 'global', 'Read operational data'),
    ('role_ops_delete', 'ops:delete', 'global', 'Delete operational data'),
    ('role_gdpr_erase', 'compliance:gdpr-erase-admin', 'global', 'GDPR right-to-be-forgotten admin');

INSERT OR IGNORE INTO policies (role_id, resource, action) VALUES
    ('role_memory_read', 'memory', 'read'),
    ('role_memory_write', 'memory', 'write'),
    ('role_memory_delete', 'memory', 'delete'),
    ('role_memory_admin', 'memory', 'admin'),
    ('role_memory_admin', 'memory', 'read'),
    ('role_memory_admin', 'memory', 'write'),
    ('role_memory_admin', 'memory', 'delete'),
    ('role_memory_export', 'memory', 'export'),
    ('role_ops_read', 'ops', 'read'),
    ('role_ops_delete', 'ops', 'delete'),
    ('role_gdpr_erase', 'memory', 'admin');
