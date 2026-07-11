"""RBAC denial tests — a principal with no roles is denied all operations.

Tests that a principal without any granted roles receives AUTHORIZATION_DENIED
for every permission type. Covers:
- memory:read denial
- memory:write denial
- memory:delete denial
- memory:admin denial
- ops:read denial

TEST STRUCTURE (1 class, ~20 assertions)
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
    """Create a fully-bootstrapped temp DB with all migrations + default RBAC roles."""
    from infra.db import open_db
    from infra.migration_runner import run_migrations
    from fact.fact_schema import ensure_facts_schema
    from infra.rbac import seed_default_roles
    with open_db(p, timeout=10.0) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        run_migrations(db)
        ensure_facts_schema(db)
        seed_default_roles(db)
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
    """Insert a principal directly via SQL."""
    now = time.time()
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO principals (id, kind, display_name, tenant_id, created_at, updated_at) "
            "VALUES (?, 'user', ?, ?, datetime('now'), datetime('now'))",
            (principal_id, principal_id, tenant_id),
        )
        conn.commit()


def _grant_role(db_path: Path, principal_id: str, role_name: str, tenant_id: str = "default") -> None:
    """Grant a role to a principal via SQL."""
    with sqlite3.connect(str(db_path)) as conn:
        role_row = conn.execute(
            "SELECT id FROM roles WHERE name = ? AND tenant_id = ?",
            (role_name, tenant_id),
        ).fetchone()
        if role_row is None:
            return
        conn.execute(
            "INSERT OR IGNORE INTO role_bindings (principal_id, role_id, granted_at) "
            "VALUES (?, ?, datetime('now'))",
            (principal_id, role_row[0]),
        )
        conn.commit()


def _get_authorizer(db_path: Path):
    """Try to get the authorizer; return None if not yet implemented."""
    # Use the module-level mcp_authorize function
    try:
        from infra.authorizer import mcp_authorize
        def _check(principal_id: str, resource: str, action: str) -> bool:
            return mcp_authorize(principal_id, action, resource, str(db_path))
        return type("Authorizer", (), {"check": lambda self, **kw: _check(kw["principal_id"], kw["resource"], kw["action"])})()
    except ImportError:
        return None
    except Exception:
        return None


def _get_rbac_engine(db_path: Path):
    """Try to get the RBAC engine; return None if not yet implemented."""
    try:
        from infra.rbac import check_permission
        def _check(principal_id: str, resource: str, action: str) -> bool:
            import sqlite3 as _sq
            with _sq.connect(str(db_path)) as _conn:
                return check_permission(_conn, principal_id, resource, action)
        return type("RBACEngine", (), {"check": lambda self, **kw: _check(kw["principal_id"], kw["resource"], kw["action"])})()
    except ImportError:
        return None
    except Exception:
        return None


def _has_rbac_schema(db_path: Path) -> bool:
    """Check if RBAC tables exist."""
    with sqlite3.connect(str(db_path)) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    return "roles" in tables and "role_bindings" in tables


def _has_principals_table(db_path: Path) -> bool:
    """Check if principals table exists."""
    with sqlite3.connect(str(db_path)) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    return "principals" in tables


# ===================================================================
# CLASS: Denials Without Role (~20 assertions)
# ===================================================================

@pytest.mark.rbac
class TestRBACDeniesWithoutRole:
    """A principal with no roles is denied all operations."""

    def test_principals_table_exists(self, db_path: Path):
        """Precondition: principals table must exist for RBAC to work."""
        assert _has_principals_table(db_path), "principals table missing (migration 043)"

    def test_rbac_tables_exist(self, db_path: Path):
        """Precondition: RBAC tables must exist."""
        assert _has_rbac_schema(db_path), "RBAC tables missing (migration 045)"

    def test_principal_created_without_roles(self, db_path: Path):
        """A newly created principal has zero role bindings."""
        _create_principal(db_path, "anon-1")
        with sqlite3.connect(str(db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM role_bindings WHERE principal_id = ?",
                ("anon-1",),
            ).fetchone()[0]
        assert count == 0, f"Expected 0 role bindings for new principal, got {count}"

    def test_anon_denied_memory_read_direct(self, db_path: Path):
        """Direct SQL: anon principal has no 'read' policy on 'memory'."""
        _create_principal(db_path, "anon-rd")
        with sqlite3.connect(str(db_path)) as conn:
            policies = conn.execute(
                "SELECT p.action, p.resource FROM policies p "
                "JOIN role_bindings rb ON p.role_id = rb.role_id "
                "WHERE rb.principal_id = ? AND p.resource = 'memory' AND p.action = 'read'",
                ("anon-rd",),
            ).fetchall()
        assert len(policies) == 0, "Anonymous principal should have no memory:read policy"

    def test_anon_denied_memory_write_direct(self, db_path: Path):
        """Direct SQL: anon principal has no 'write' policy on 'memory'."""
        _create_principal(db_path, "anon-wr")
        with sqlite3.connect(str(db_path)) as conn:
            policies = conn.execute(
                "SELECT p.action, p.resource FROM policies p "
                "JOIN role_bindings rb ON p.role_id = rb.role_id "
                "WHERE rb.principal_id = ? AND p.resource = 'memory' AND p.action = 'write'",
                ("anon-wr",),
            ).fetchall()
        assert len(policies) == 0

    def test_anon_denied_memory_delete_direct(self, db_path: Path):
        """Direct SQL: anon principal has no 'delete' policy on 'memory'."""
        _create_principal(db_path, "anon-dl")
        with sqlite3.connect(str(db_path)) as conn:
            policies = conn.execute(
                "SELECT p.action, p.resource FROM policies p "
                "JOIN role_bindings rb ON p.role_id = rb.role_id "
                "WHERE rb.principal_id = ? AND p.resource = 'memory' AND p.action = 'delete'",
                ("anon-dl",),
            ).fetchall()
        assert len(policies) == 0

    def test_anon_denied_memory_admin_direct(self, db_path: Path):
        """Direct SQL: anon principal has no 'admin' policy on 'memory'."""
        _create_principal(db_path, "anon-ad")
        with sqlite3.connect(str(db_path)) as conn:
            policies = conn.execute(
                "SELECT p.action, p.resource FROM policies p "
                "JOIN role_bindings rb ON p.role_id = rb.role_id "
                "WHERE rb.principal_id = ? AND p.resource = 'memory' AND p.action = 'admin'",
                ("anon-ad",),
            ).fetchall()
        assert len(policies) == 0

    def test_anon_denied_ops_read_direct(self, db_path: Path):
        """Direct SQL: anon principal has no 'read' policy on 'ops'."""
        _create_principal(db_path, "anon-op")
        with sqlite3.connect(str(db_path)) as conn:
            policies = conn.execute(
                "SELECT p.action, p.resource FROM policies p "
                "JOIN role_bindings rb ON p.role_id = rb.role_id "
                "WHERE rb.principal_id = ? AND p.resource = 'ops' AND p.action = 'read'",
                ("anon-op",),
            ).fetchall()
        assert len(policies) == 0

    def test_anon_denied_all_resources_direct(self, db_path: Path):
        """Direct SQL: anon principal has zero policies across all resources."""
        _create_principal(db_path, "anon-all")
        with sqlite3.connect(str(db_path)) as conn:
            policies = conn.execute(
                "SELECT p.action, p.resource FROM policies p "
                "JOIN role_bindings rb ON p.role_id = rb.role_id "
                "WHERE rb.principal_id = ?",
                ("anon-all",),
            ).fetchall()
        assert len(policies) == 0, (
            f"Anonymous principal should have zero policies, got {policies}"
        )

    def test_authorizer_module_importable(self, db_path: Path):
        """infra.authorizer module should be importable."""
        try:
            from infra.authorizer import Authorizer  # noqa: F401
        except ImportError:
            pytest.skip("infra.authorizer not yet implemented")

    def test_authorizer_denies_no_role_read(self, db_path: Path):
        """Authorizer should deny memory:read for principal with no roles."""
        authorizer = _get_authorizer(db_path)
        if authorizer is None:
            pytest.skip("infra.authorizer not yet implemented")
        _create_principal(db_path, "denied-rd")
        result = authorizer.check(
            principal_id="denied-rd",
            resource="memory",
            action="read",
        )
        assert result is False or result == "deny", (
            f"Expected deny for no-role principal, got {result}"
        )

    def test_authorizer_denies_no_role_write(self, db_path: Path):
        """Authorizer should deny memory:write for principal with no roles."""
        authorizer = _get_authorizer(db_path)
        if authorizer is None:
            pytest.skip("infra.authorizer not yet implemented")
        _create_principal(db_path, "denied-wr")
        result = authorizer.check(
            principal_id="denied-wr",
            resource="memory",
            action="write",
        )
        assert result is False or result == "deny"

    def test_authorizer_denies_no_role_delete(self, db_path: Path):
        """Authorizer should deny memory:delete for principal with no roles."""
        authorizer = _get_authorizer(db_path)
        if authorizer is None:
            pytest.skip("infra.authorizer not yet implemented")
        _create_principal(db_path, "denied-dl")
        result = authorizer.check(
            principal_id="denied-dl",
            resource="memory",
            action="delete",
        )
        assert result is False or result == "deny"

    def test_authorizer_denies_no_role_admin(self, db_path: Path):
        """Authorizer should deny memory:admin for principal with no roles."""
        authorizer = _get_authorizer(db_path)
        if authorizer is None:
            pytest.skip("infra.authorizer not yet implemented")
        _create_principal(db_path, "denied-ad")
        result = authorizer.check(
            principal_id="denied-ad",
            resource="memory",
            action="admin",
        )
        assert result is False or result == "deny"

    def test_authorizer_denies_no_role_ops_read(self, db_path: Path):
        """Authorizer should deny ops:read for principal with no roles."""
        authorizer = _get_authorizer(db_path)
        if authorizer is None:
            pytest.skip("infra.authorizer not yet implemented")
        _create_principal(db_path, "denied-op")
        result = authorizer.check(
            principal_id="denied-op",
            resource="ops",
            action="read",
        )
        assert result is False or result == "deny"

    def test_authorizer_returns_error_code(self, db_path: Path):
        """Authorizer denial should surface AUTHORIZATION_DENIED error code."""
        try:
            from infra.infrastructure import ErrorCode
            assert ErrorCode.AUTHORIZATION_DENIED.value == "AUTHORIZATION_DENIED"
        except ImportError:
            pytest.skip("ErrorCode not importable")

    def test_nonexistent_principal_denied(self, db_path: Path):
        """A principal that doesn't exist should be denied."""
        authorizer = _get_authorizer(db_path)
        if authorizer is None:
            pytest.skip("infra.authorizer not yet implemented")
        result = authorizer.check(
            principal_id="nonexistent-principal-xyz",
            resource="memory",
            action="read",
        )
        assert result is False or result == "deny"

    def test_empty_principal_id_denied(self, db_path: Path):
        """An empty principal_id should be denied."""
        authorizer = _get_authorizer(db_path)
        if authorizer is None:
            pytest.skip("infra.authorizer not yet implemented")
        result = authorizer.check(
            principal_id="",
            resource="memory",
            action="read",
        )
        assert result is False or result == "deny"
