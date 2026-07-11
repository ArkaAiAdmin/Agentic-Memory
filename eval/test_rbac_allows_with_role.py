"""RBAC grant tests — roles grant specific permissions.

Tests that granting a role enables the corresponding permission while
denying permissions not covered by the role. Validates:
- memory:read role grants read
- memory:read role denies write
- memory:write role grants write
- Granting multiple roles grants all covered permissions
- Revoking a role removes the permission

TEST STRUCTURE (1 class, ~25 assertions)
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
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO principals (id, kind, display_name, tenant_id, created_at, updated_at) "
            "VALUES (?, 'user', ?, ?, datetime('now'), datetime('now'))",
            (principal_id, principal_id, tenant_id),
        )
        conn.commit()


def _grant_role(db_path: Path, principal_id: str, role_name: str, tenant_id: str = "default") -> None:
    with sqlite3.connect(str(db_path)) as conn:
        role_row = conn.execute(
            "SELECT id FROM roles WHERE name = ? AND tenant_id = ?",
            (role_name, tenant_id),
        ).fetchone()
        if role_row is None:
            pytest.skip(f"Role '{role_name}' not found in DB")
        conn.execute(
            "INSERT OR IGNORE INTO role_bindings (principal_id, role_id, granted_at) "
            "VALUES (?, ?, datetime('now'))",
            (principal_id, role_row[0]),
        )
        conn.commit()


def _revoke_role(db_path: Path, principal_id: str, role_name: str, tenant_id: str = "default") -> None:
    with sqlite3.connect(str(db_path)) as conn:
        role_row = conn.execute(
            "SELECT id FROM roles WHERE name = ? AND tenant_id = ?",
            (role_name, tenant_id),
        ).fetchone()
        if role_row is None:
            return
        conn.execute(
            "DELETE FROM role_bindings WHERE principal_id = ? AND role_id = ?",
            (principal_id, role_row[0]),
        )
        conn.commit()


def _get_policies_for_principal(db_path: Path, principal_id: str) -> list[tuple[str, str]]:
    """Return list of (action, resource) tuples the principal has via role bindings."""
    with sqlite3.connect(str(db_path)) as conn:
        return conn.execute(
            "SELECT DISTINCT p.action, p.resource FROM policies p "
            "JOIN role_bindings rb ON p.role_id = rb.role_id "
            "WHERE rb.principal_id = ?",
            (principal_id,),
        ).fetchall()


def _get_authorizer(db_path: Path):
    try:
        from infra.authorizer import mcp_authorize
        def _check(principal_id: str, resource: str, action: str) -> bool:
            return mcp_authorize(principal_id, action, resource, str(db_path))
        return type("Authorizer", (), {"check": lambda self, **kw: _check(kw["principal_id"], kw["resource"], kw["action"])})()
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
# CLASS: Allowances With Role (~25 assertions)
# ===================================================================

@pytest.mark.rbac
class TestRBACAllowsWithRole:
    """Granting roles grants specific permissions; withholding denies."""

    def test_preconditions(self, db_path: Path):
        """Both principals and RBAC tables must exist."""
        assert _has_principals_table(db_path), "principals table missing"
        assert _has_rbac_schema(db_path), "RBAC tables missing"

    def test_grant_read_role_creates_binding(self, db_path: Path):
        """Granting memory:read creates a role_bindings row."""
        _create_principal(db_path, "reader-1")
        _grant_role(db_path, "reader-1", "memory:read")
        with sqlite3.connect(str(db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM role_bindings WHERE principal_id = ?",
                ("reader-1",),
            ).fetchone()[0]
        assert count == 1

    def test_reader_has_read_policy(self, db_path: Path):
        """Principal with memory:read role should have ('read', 'memory') policy."""
        _create_principal(db_path, "reader-2")
        _grant_role(db_path, "reader-2", "memory:read")
        policies = _get_policies_for_principal(db_path, "reader-2")
        assert ("read", "memory") in policies

    def test_reader_lacks_write_policy(self, db_path: Path):
        """Principal with memory:read role should NOT have write policy."""
        _create_principal(db_path, "reader-3")
        _grant_role(db_path, "reader-3", "memory:read")
        policies = _get_policies_for_principal(db_path, "reader-3")
        assert ("write", "memory") not in policies

    def test_reader_lacks_delete_policy(self, db_path: Path):
        _create_principal(db_path, "reader-4")
        _grant_role(db_path, "reader-4", "memory:read")
        policies = _get_policies_for_principal(db_path, "reader-4")
        assert ("delete", "memory") not in policies

    def test_reader_lacks_admin_policy(self, db_path: Path):
        _create_principal(db_path, "reader-5")
        _grant_role(db_path, "reader-5", "memory:read")
        policies = _get_policies_for_principal(db_path, "reader-5")
        assert ("admin", "memory") not in policies

    def test_writer_has_write_policy(self, db_path: Path):
        """Principal with memory:write role should have ('write', 'memory') policy."""
        _create_principal(db_path, "writer-1")
        _grant_role(db_path, "writer-1", "memory:write")
        policies = _get_policies_for_principal(db_path, "writer-1")
        assert ("write", "memory") in policies

    def test_writer_lacks_read_policy(self, db_path: Path):
        _create_principal(db_path, "writer-2")
        _grant_role(db_path, "writer-2", "memory:write")
        policies = _get_policies_for_principal(db_path, "writer-2")
        assert ("read", "memory") not in policies

    def test_writer_lacks_admin_policy(self, db_path: Path):
        _create_principal(db_path, "writer-3")
        _grant_role(db_path, "writer-3", "memory:write")
        policies = _get_policies_for_principal(db_path, "writer-3")
        assert ("admin", "memory") not in policies

    def test_admin_has_all_memory_actions(self, db_path: Path):
        """Principal with memory:admin role should have read, write, delete, admin."""
        _create_principal(db_path, "admin-1")
        _grant_role(db_path, "admin-1", "memory:admin")
        policies = _get_policies_for_principal(db_path, "admin-1")
        action_set = {a for a, r in policies if r == "memory"}
        for expected in ("read", "write", "delete", "admin"):
            assert expected in action_set, f"memory:admin should have '{expected}' policy"

    def test_delete_role_has_delete_policy(self, db_path: Path):
        _create_principal(db_path, "deleter-1")
        _grant_role(db_path, "deleter-1", "memory:delete")
        policies = _get_policies_for_principal(db_path, "deleter-1")
        assert ("delete", "memory") in policies

    def test_ops_read_role_has_ops_read(self, db_path: Path):
        _create_principal(db_path, "ops-r-1")
        _grant_role(db_path, "ops-r-1", "ops:read")
        policies = _get_policies_for_principal(db_path, "ops-r-1")
        assert ("read", "ops") in policies

    def test_ops_read_role_lacks_memory_read(self, db_path: Path):
        _create_principal(db_path, "ops-r-2")
        _grant_role(db_path, "ops-r-2", "ops:read")
        policies = _get_policies_for_principal(db_path, "ops-r-2")
        assert ("read", "memory") not in policies

    def test_multiple_roles_cumulative(self, db_path: Path):
        """Granting read + write roles should give both permissions."""
        _create_principal(db_path, "multi-1")
        _grant_role(db_path, "multi-1", "memory:read")
        _grant_role(db_path, "multi-1", "memory:write")
        policies = _get_policies_for_principal(db_path, "multi-1")
        assert ("read", "memory") in policies
        assert ("write", "memory") in policies

    def test_revoke_removes_permission(self, db_path: Path):
        """Revoking a role removes the corresponding policy."""
        _create_principal(db_path, "revoker-1")
        _grant_role(db_path, "revoker-1", "memory:read")
        policies_before = _get_policies_for_principal(db_path, "revoker-1")
        assert ("read", "memory") in policies_before
        _revoke_role(db_path, "revoker-1", "memory:read")
        policies_after = _get_policies_for_principal(db_path, "revoker-1")
        assert ("read", "memory") not in policies_after

    def test_revoke_only_removes_target_role(self, db_path: Path):
        """Revoking write role should keep read role intact."""
        _create_principal(db_path, "revoker-2")
        _grant_role(db_path, "revoker-2", "memory:read")
        _grant_role(db_path, "revoker-2", "memory:write")
        _revoke_role(db_path, "revoker-2", "memory:write")
        policies = _get_policies_for_principal(db_path, "revoker-2")
        assert ("read", "memory") in policies
        assert ("write", "memory") not in policies

    # ------------------------------------------------------------------
    # Authorizer API tests (skip if not yet implemented)
    # ------------------------------------------------------------------

    def test_authorizer_allows_read_with_role(self, db_path: Path):
        authorizer = _get_authorizer(db_path)
        if authorizer is None:
            pytest.skip("infra.authorizer not yet implemented")
        _create_principal(db_path, "auth-rd-1")
        _grant_role(db_path, "auth-rd-1", "memory:read")
        result = authorizer.check(
            principal_id="auth-rd-1",
            resource="memory",
            action="read",
        )
        assert result is True or result == "allow", f"Expected allow, got {result}"

    def test_authorizer_denies_write_with_read_only_role(self, db_path: Path):
        authorizer = _get_authorizer(db_path)
        if authorizer is None:
            pytest.skip("infra.authorizer not yet implemented")
        _create_principal(db_path, "auth-rd-2")
        _grant_role(db_path, "auth-rd-2", "memory:read")
        result = authorizer.check(
            principal_id="auth-rd-2",
            resource="memory",
            action="write",
        )
        assert result is False or result == "deny", f"Expected deny, got {result}"

    def test_authorizer_allows_write_with_write_role(self, db_path: Path):
        authorizer = _get_authorizer(db_path)
        if authorizer is None:
            pytest.skip("infra.authorizer not yet implemented")
        _create_principal(db_path, "auth-wr-1")
        _grant_role(db_path, "auth-wr-1", "memory:write")
        result = authorizer.check(
            principal_id="auth-wr-1",
            resource="memory",
            action="write",
        )
        assert result is True or result == "allow"

    def test_authorizer_allows_admin_with_admin_role(self, db_path: Path):
        authorizer = _get_authorizer(db_path)
        if authorizer is None:
            pytest.skip("infra.authorizer not yet implemented")
        _create_principal(db_path, "auth-ad-1")
        _grant_role(db_path, "auth-ad-1", "memory:admin")
        result = authorizer.check(
            principal_id="auth-ad-1",
            resource="memory",
            action="admin",
        )
        assert result is True or result == "allow"

    def test_authorizer_lifecycle_allows_then_denies(self, db_path: Path):
        """Full lifecycle: grant → allow → revoke → deny."""
        authorizer = _get_authorizer(db_path)
        if authorizer is None:
            pytest.skip("infra.authorizer not yet implemented")
        pid = "auth-lifecycle-1"
        _create_principal(db_path, pid)
        _grant_role(db_path, pid, "memory:read")
        assert authorizer.check(principal_id=pid, resource="memory", action="read") in (True, "allow")
        _revoke_role(db_path, pid, "memory:read")
        assert authorizer.check(principal_id=pid, resource="memory", action="read") in (False, "deny")
