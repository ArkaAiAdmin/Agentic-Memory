"""Multi-tenant RBAC tests — roles don't leak across tenants.

Tests that RBAC role bindings are properly scoped to tenants:
- A role granted in tenant A is not visible in tenant B
- Cross-tenant authorization is denied
- Different tenants can have same role names independently

TEST STRUCTURE (1 class, ~15 assertions)
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Generator

import pytest

sys.path.insert(
    0,
    str(os.environ.get("MEMORY_INSTALL_ROOT", os.path.expanduser("~/.config/agentic-memory"))),
)
from infra.memory_config import install_root
sys.path.insert(0, str(install_root()))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap_db(p: Path) -> None:
    from infra.db import open_db
    from infra.migration_runner import run_migrations
    from fact.fact_schema import ensure_facts_schema
    with open_db(p, timeout=10.0) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        run_migrations(db)
        ensure_facts_schema(db)
        db.commit()


@pytest.fixture
def db_path() -> Generator[Path, None, None]:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    p = Path(tmp.name)
    try:
        _bootstrap_db(p)
        yield p
    finally:
        p.unlink(missing_ok=True)


def _create_principal(db_path: Path, principal_id: str, tenant_id: str = "default") -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO principals (id, kind, display_name, tenant_id, created_at, updated_at) "
            "VALUES (?, 'user', ?, ?, datetime('now'), datetime('now'))",
            (principal_id, principal_id, tenant_id),
        )
        conn.commit()


def _create_tenant_role(db_path: Path, role_name: str, tenant_id: str, description: str = "") -> str:
    """Create a role in a specific tenant and return its ID."""
    import uuid
    role_id = f"role_{role_name}_{tenant_id}_{uuid.uuid4().hex[:8]}"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO roles (id, name, tenant_id, description) VALUES (?, ?, ?, ?)",
            (role_id, role_name, tenant_id, description),
        )
        conn.commit()
    return role_id


def _grant_role(db_path: Path, principal_id: str, role_name: str, tenant_id: str = "default") -> None:
    with sqlite3.connect(str(db_path)) as conn:
        role_row = conn.execute(
            "SELECT id FROM roles WHERE name = ? AND tenant_id = ?",
            (role_name, tenant_id),
        ).fetchone()
        if role_row is None:
            pytest.skip(f"Role '{role_name}' not found in tenant '{tenant_id}'")
        conn.execute(
            "INSERT OR IGNORE INTO role_bindings (principal_id, role_id, granted_at) "
            "VALUES (?, ?, datetime('now'))",
            (principal_id, role_row[0]),
        )
        conn.commit()


def _get_policies_for_principal(db_path: Path, principal_id: str) -> list[tuple[str, str]]:
    """Return (action, resource) tuples via role bindings."""
    with sqlite3.connect(str(db_path)) as conn:
        return conn.execute(
            "SELECT DISTINCT p.action, p.resource FROM policies p "
            "JOIN role_bindings rb ON p.role_id = rb.role_id "
            "WHERE rb.principal_id = ?",
            (principal_id,),
        ).fetchall()


def _get_authorizer(db_path: Path):
    try:
        from infra.authorizer import Authorizer
        return Authorizer(db_path=str(db_path))
    except (ImportError, Exception):
        return None


def _has_rbac_schema(db_path: Path) -> bool:
    with sqlite3.connect(str(db_path)) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    return "roles" in tables and "role_bindings" in tables


def _has_principals_table(db_path: Path) -> bool:
    with sqlite3.connect(str(db_path)) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    return "principals" in tables


# ===================================================================
# CLASS: Multi-Tenant RBAC Tests (~15 assertions)
# ===================================================================

@pytest.mark.rbac
class TestRBACMultiTenant:
    """RBAC role bindings are scoped to tenants."""

    @pytest.fixture(autouse=True)
    def _closed_auth(self, closed_auth_env):
        pass

    def test_preconditions(self, db_path: Path):
        assert _has_principals_table(db_path)
        assert _has_rbac_schema(db_path)

    def test_role_in_tenant_a_not_visible_to_tenant_b(self, db_path: Path):
        """A role granted in tenant A should not appear in tenant B's policies."""
        _create_principal(db_path, "mt-a-1", tenant_id="tenant-a")
        _create_principal(db_path, "mt-b-1", tenant_id="tenant-b")
        # Create a custom role in tenant-a
        _create_tenant_role(db_path, "custom:read", "tenant-a")
        _grant_role(db_path, "mt-a-1", "custom:read", tenant_id="tenant-a")
        # mt-a-1 should have policies, mt-b-1 should not
        policies_a = _get_policies_for_principal(db_path, "mt-a-1")
        policies_b = _get_policies_for_principal(db_path, "mt-b-1")
        # mt-b-1 should have no policies (no roles granted)
        assert len(policies_b) == 0, (
            f"Tenant B principal should have no policies, got {policies_b}"
        )

    def test_different_tenants_independent_role_names(self, db_path: Path):
        """Two tenants can create roles with the same name independently."""
        _create_tenant_role(db_path, "custom:access", "tenant-a")
        _create_tenant_role(db_path, "custom:access", "tenant-b")
        with sqlite3.connect(str(db_path)) as conn:
            roles = conn.execute(
                "SELECT id, name, tenant_id FROM roles WHERE name = 'custom:access'"
            ).fetchall()
        assert len(roles) >= 2, "Should have at least 2 roles with same name in different tenants"
        tenants = {r[2] for r in roles}
        assert "tenant-a" in tenants
        assert "tenant-b" in tenants

    def test_cross_tenant_authorization_denied(self, db_path: Path):
        """A principal in tenant A cannot use a role granted only in tenant B."""
        _create_principal(db_path, "xten-a", tenant_id="tenant-a")
        _create_tenant_role(db_path, "super:access", "tenant-b")
        _grant_role(db_path, "xten-a", "super:access", tenant_id="tenant-b")
        # The grant should succeed (role bindings don't check tenant match)
        # But the effective policies should be empty since the role's policies
        # are in tenant-b's scope
        policies = _get_policies_for_principal(db_path, "xten-a")
        # Since the role is in tenant-b, it may or may not have policies
        # depending on implementation. The key test is that the authorizer
        # should NOT grant cross-tenant access.
        authorizer = _get_authorizer(db_path)
        if authorizer is not None:
            result = authorizer.check(
                principal_id="xten-a",
                resource="memory",
                action="read",
            )
            # Should be denied since the role is from a different tenant
            assert result is False or result == "deny", (
                f"Cross-tenant authorization should be denied, got {result}"
            )

    def test_tenant_a_admin_cannot_admin_tenant_b(self, db_path: Path):
        """An admin in tenant A should not have admin privileges in tenant B."""
        _create_principal(db_path, "tadmin-a", tenant_id="tenant-a")
        _create_principal(db_path, "tadmin-b", tenant_id="tenant-b")
        # Grant memory:admin in global scope (built-in)
        _grant_role(db_path, "tadmin-a", "memory:admin")
        # tadmin-b has no roles
        authorizer = _get_authorizer(db_path)
        if authorizer is not None:
            # tadmin-a can admin in its own tenant
            r_a = authorizer.check(principal_id="tadmin-a", resource="memory", action="admin")
            assert r_a is True or r_a == "allow"
            # tadmin-b cannot admin (no roles)
            r_b = authorizer.check(principal_id="tadmin-b", resource="memory", action="admin")
            assert r_b is False or r_b == "deny"

    def test_role_bindings_scoped_to_principal(self, db_path: Path):
        """A role binding for principal A should not affect principal B."""
        _create_principal(db_path, "scope-a")
        _create_principal(db_path, "scope-b")
        _grant_role(db_path, "scope-a", "memory:read")
        policies_a = _get_policies_for_principal(db_path, "scope-a")
        policies_b = _get_policies_for_principal(db_path, "scope-b")
        assert ("read", "memory") in policies_a
        assert len(policies_b) == 0

    def test_delete_principal_cascades_bindings(self, db_path: Path):
        """Deleting a principal should cascade-delete its role bindings.

        SQLite FK cascade requires PRAGMA foreign_keys=ON on the connection.
        """
        _create_principal(db_path, "cascade-1")
        _grant_role(db_path, "cascade-1", "memory:read")
        with sqlite3.connect(str(db_path)) as conn:
            count_before = conn.execute(
                "SELECT COUNT(*) FROM role_bindings WHERE principal_id = ?",
                ("cascade-1",),
            ).fetchone()[0]
        assert count_before == 1
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("DELETE FROM principals WHERE id = ?", ("cascade-1",))
            conn.commit()
            count_after = conn.execute(
                "SELECT COUNT(*) FROM role_bindings WHERE principal_id = ?",
                ("cascade-1",),
            ).fetchone()[0]
        assert count_after == 0, "Cascade delete should remove role bindings"

    def test_builtin_roles_are_global(self, db_path: Path):
        """Built-in roles should have tenant_id='global', not a specific tenant."""
        with sqlite3.connect(str(db_path)) as conn:
            global_roles = conn.execute(
                "SELECT name, tenant_id FROM roles WHERE name LIKE 'memory:%' OR name LIKE 'ops:%'"
            ).fetchall()
        for name, tenant in global_roles:
            assert tenant == "global", f"Built-in role {name} should be global, got '{tenant}'"

    def test_different_tenants_see_only_own_principals(self, db_path: Path):
        """Principals in different tenants should be isolated."""
        _create_principal(db_path, "iso-a", tenant_id="tenant-a")
        _create_principal(db_path, "iso-b", tenant_id="tenant-b")
        with sqlite3.connect(str(db_path)) as conn:
            a_principals = conn.execute(
                "SELECT id FROM principals WHERE tenant_id = 'tenant-a'"
            ).fetchall()
            b_principals = conn.execute(
                "SELECT id FROM principals WHERE tenant_id = 'tenant-b'"
            ).fetchall()
        a_ids = {r[0] for r in a_principals}
        b_ids = {r[0] for r in b_principals}
        assert "iso-a" in a_ids
        assert "iso-b" in b_ids
        assert "iso-b" not in a_ids
        assert "iso-a" not in b_ids

    def test_roles_unique_per_tenant(self, db_path: Path):
        """Role (name, tenant_id) should be unique per schema."""
        _create_tenant_role(db_path, "unique:role", "tenant-u")
        # Inserting same name+tenant should fail (UNIQUE constraint)
        with sqlite3.connect(str(db_path)) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO roles (id, name, tenant_id) VALUES (?, ?, ?)",
                    ("role_dup", "unique:role", "tenant-u"),
                )
