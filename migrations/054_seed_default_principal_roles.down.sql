-- 054 down: remove the bindings seeded by 054 for the built-in 'default' principal.
--
-- Only the two bindings created by this migration are removed (matched by the
-- 'migration:054' grant marker) so any bindings granted to 'default' by an
-- operator survive rollback. The 'default' principal row is intentionally left
-- in place: other rows may reference it, and an empty principal is harmless.

DELETE FROM role_bindings
    WHERE principal_id = 'default'
      AND role_id IN ('role_memory_admin', 'role_ops_admin')
      AND granted_by = 'migration:054';

DELETE FROM policies WHERE role_id = 'role_ops_admin';
DELETE FROM roles WHERE id = 'role_ops_admin';
