-- 045 down: Drop RBAC tables in reverse order.

DROP TABLE IF EXISTS principal_roles_audit;
DROP TABLE IF EXISTS acl_overrides;
DROP TABLE IF EXISTS policies;
DROP TABLE IF EXISTS role_bindings;
DROP TABLE IF EXISTS roles;
