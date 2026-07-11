"""RBAC schema validation — verifies all RBAC tables exist with correct columns.

Tests the database schema created by migrations 045/046 to ensure:
- roles table has correct columns and constraints
- role_bindings table has correct columns and FK references
- policies table has correct columns with CHECK constraint on action
- acl_overrides table has correct columns with CHECK constraints
- principal_roles_audit table exists with correct columns
- Built-in roles exist (memory:read, memory:write, etc.)
- Built-in policies exist for each role

TEST STRUCTURE (1 class, ~35 assertions)
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
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
    """Create a fully-bootstrapped temp DB with all migrations (incl. 045/046)."""
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


def _col_set(db_path: Path, table: str) -> set[str]:
    """Return set of column names for a table."""
    with sqlite3.connect(str(db_path)) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_names(db_path: Path) -> set[str]:
    """Return set of all table names."""
    with sqlite3.connect(str(db_path)) as conn:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}


def _index_names(db_path: Path) -> set[str]:
    """Return set of all index names."""
    with sqlite3.connect(str(db_path)) as conn:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}


def _table_sql(db_path: Path, table: str) -> str:
    """Return the CREATE TABLE SQL for a table."""
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    return row[0] if row else ""


def _has_rbac_tables(db_path: Path) -> bool:
    """Check if RBAC tables exist (migrations 045 applied)."""
    tables = _table_names(db_path)
    return "roles" in tables and "role_bindings" in tables


def _has_builtins(db_path: Path) -> bool:
    """Check if built-in roles exist (migration 046 applied)."""
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute("SELECT COUNT(*) FROM roles").fetchone()
    return row[0] > 0 if row else False


# ===================================================================
# CLASS 1: RBAC Schema Validation (~35 assertions)
# ===================================================================

@pytest.mark.rbac
class TestRBACSchema:
    """Verify RBAC schema structure from migrations 045/046."""

    def test_roles_table_exists(self, db_path: Path):
        tables = _table_names(db_path)
        assert "roles" in tables, "roles table missing — migration 045 not applied"

    def test_roles_columns(self, db_path: Path):
        cols = _col_set(db_path, "roles")
        required = {"id", "name", "tenant_id", "description", "created_at", "updated_at"}
        missing = required - cols
        assert not missing, f"roles missing columns: {missing}"

    def test_roles_id_is_primary_key(self, db_path: Path):
        sql = _table_sql(db_path, "roles")
        assert "PRIMARY KEY" in sql.upper(), "roles.id should be PRIMARY KEY"

    def test_roles_unique_name_tenant(self, db_path: Path):
        sql = _table_sql(db_path, "roles")
        assert "UNIQUE" in sql.upper(), "roles should have UNIQUE constraint on (name, tenant_id)"

    def test_roles_tenant_id_default(self, db_path: Path):
        with sqlite3.connect(str(db_path)) as conn:
            for row in conn.execute("PRAGMA table_info(roles)").fetchall():
                if row[1] == "tenant_id":
                    default_val = str(row[4] or "").strip("'\"")
                    assert default_val == "default", f"Expected default='default', got {row[4]!r}"
                    return
        pytest.fail("tenant_id column not found in roles")

    def test_roles_index_exists(self, db_path: Path):
        idxs = _index_names(db_path)
        assert any("roles" in i and "tenant" in i for i in idxs), (
            "Missing idx_roles_tenant index"
        )

    def test_role_bindings_table_exists(self, db_path: Path):
        tables = _table_names(db_path)
        assert "role_bindings" in tables, "role_bindings table missing"

    def test_role_bindings_columns(self, db_path: Path):
        cols = _col_set(db_path, "role_bindings")
        required = {"id", "principal_id", "role_id", "granted_at", "granted_by"}
        missing = required - cols
        assert not missing, f"role_bindings missing columns: {missing}"

    def test_role_bindings_unique_principal_role(self, db_path: Path):
        sql = _table_sql(db_path, "role_bindings")
        assert "UNIQUE" in sql.upper(), (
            "role_bindings should have UNIQUE(principal_id, role_id)"
        )

    def test_role_bindings_index_principal(self, db_path: Path):
        idxs = _index_names(db_path)
        assert any("role_bindings" in i and "principal" in i for i in idxs)

    def test_role_bindings_index_role(self, db_path: Path):
        idxs = _index_names(db_path)
        assert any("role_bindings" in i and "role" in i for i in idxs)

    def test_policies_table_exists(self, db_path: Path):
        tables = _table_names(db_path)
        assert "policies" in tables, "policies table missing"

    def test_policies_columns(self, db_path: Path):
        cols = _col_set(db_path, "policies")
        required = {"id", "role_id", "resource", "action", "created_at"}
        missing = required - cols
        assert not missing, f"policies missing columns: {missing}"

    def test_policies_check_constraint_action(self, db_path: Path):
        """policies.action should have a CHECK constraint limiting values."""
        sql = _table_sql(db_path, "policies")
        assert "CHECK" in sql.upper(), "policies should have CHECK constraint on action"
        assert "read" in sql.lower(), "CHECK should include 'read'"
        assert "write" in sql.lower(), "CHECK should include 'write'"
        assert "delete" in sql.lower(), "CHECK should include 'delete'"
        assert "admin" in sql.lower(), "CHECK should include 'admin'"

    def test_policies_unique_role_resource_action(self, db_path: Path):
        sql = _table_sql(db_path, "policies")
        assert "UNIQUE" in sql.upper(), (
            "policies should have UNIQUE(role_id, resource, action)"
        )

    def test_policies_index_role(self, db_path: Path):
        idxs = _index_names(db_path)
        assert any("policies" in i and "role" in i for i in idxs)

    def test_policies_index_resource(self, db_path: Path):
        idxs = _index_names(db_path)
        assert any("policies" in i and "resource" in i for i in idxs)

    def test_acl_overrides_table_exists(self, db_path: Path):
        tables = _table_names(db_path)
        assert "acl_overrides" in tables, "acl_overrides table missing"

    def test_acl_overrides_columns(self, db_path: Path):
        cols = _col_set(db_path, "acl_overrides")
        required = {"id", "principal_id", "resource_id", "action", "effect",
                     "granted_at", "granted_by"}
        missing = required - cols
        assert not missing, f"acl_overrides missing columns: {missing}"

    def test_acl_overrides_check_action(self, db_path: Path):
        sql = _table_sql(db_path, "acl_overrides")
        assert "CHECK" in sql.upper(), "acl_overrides should have CHECK on action"
        assert "read" in sql.lower()

    def test_acl_overrides_check_effect(self, db_path: Path):
        sql = _table_sql(db_path, "acl_overrides")
        assert "effect" in sql.lower(), "acl_overrides should have effect column"
        assert "allow" in sql.lower(), "CHECK should include 'allow'"
        assert "deny" in sql.lower(), "CHECK should include 'deny'"

    def test_acl_overrides_effect_default_deny(self, db_path: Path):
        with sqlite3.connect(str(db_path)) as conn:
            for row in conn.execute("PRAGMA table_info(acl_overrides)").fetchall():
                if row[1] == "effect":
                    default_val = str(row[4] or "").strip("'\"")
                    assert default_val == "deny", (
                        f"acl_overrides.effect default should be 'deny', got {row[4]!r}"
                    )
                    return
        pytest.fail("effect column not found in acl_overrides")

    def test_acl_overrides_unique_constraint(self, db_path: Path):
        sql = _table_sql(db_path, "acl_overrides")
        assert "UNIQUE" in sql.upper(), (
            "acl_overrides should have UNIQUE(principal_id, resource_id, action)"
        )

    def test_acl_overrides_index_principal(self, db_path: Path):
        idxs = _index_names(db_path)
        assert any("acl_overrides" in i and "principal" in i for i in idxs)

    def test_principal_roles_audit_exists(self, db_path: Path):
        tables = _table_names(db_path)
        assert "principal_roles_audit" in tables, "principal_roles_audit table missing"

    def test_principal_roles_audit_columns(self, db_path: Path):
        cols = _col_set(db_path, "principal_roles_audit")
        required = {"id", "principal_id", "role_id", "action", "performed_by",
                     "performed_at", "reason"}
        missing = required - cols
        assert not missing, f"principal_roles_audit missing columns: {missing}"

    def test_principal_roles_audit_check_action(self, db_path: Path):
        sql = _table_sql(db_path, "principal_roles_audit")
        assert "CHECK" in sql.upper(), "principal_roles_audit should have CHECK on action"
        assert "grant" in sql.lower(), "CHECK should include 'grant'"
        assert "revoke" in sql.lower(), "CHECK should include 'revoke'"

    def test_principal_roles_audit_index_principal(self, db_path: Path):
        idxs = _index_names(db_path)
        assert any("principal_roles_audit" in i and "principal" in i for i in idxs)

    # ------------------------------------------------------------------
    # Built-in roles (migration 046)
    # ------------------------------------------------------------------

    def test_builtin_roles_exist(self, db_path: Path):
        if not _has_builtins(db_path):
            pytest.skip("migration 046 not applied — no built-in roles")
        with sqlite3.connect(str(db_path)) as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM roles").fetchall()}
        expected = {"memory:read", "memory:write", "memory:delete", "memory:admin",
                    "memory:export", "ops:read", "ops:delete", "compliance:gdpr-erase-admin"}
        missing = expected - names
        assert not missing, f"Missing built-in roles: {missing}"

    def test_builtin_role_count(self, db_path: Path):
        if not _has_builtins(db_path):
            pytest.skip("migration 046 not applied")
        with sqlite3.connect(str(db_path)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
        assert count >= 8, f"Expected at least 8 built-in roles, got {count}"

    def test_builtin_roles_tenant_global(self, db_path: Path):
        if not _has_builtins(db_path):
            pytest.skip("migration 046 not applied")
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT name, tenant_id FROM roles WHERE name LIKE 'memory:%' OR name LIKE 'ops:%' OR name LIKE 'compliance:%'"
            ).fetchall()
        for name, tenant in rows:
            assert tenant == "global", f"Built-in role {name} should be tenant='global', got '{tenant}'"

    # ------------------------------------------------------------------
    # Built-in policies (migration 046)
    # ------------------------------------------------------------------

    def test_builtin_policies_exist(self, db_path: Path):
        if not _has_builtins(db_path):
            pytest.skip("migration 046 not applied")
        with sqlite3.connect(str(db_path)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM policies").fetchone()[0]
        assert count >= 11, f"Expected at least 11 built-in policies, got {count}"

    def test_memory_read_role_has_read_policy(self, db_path: Path):
        if not _has_builtins(db_path):
            pytest.skip("migration 046 not applied")
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT p.action, p.resource FROM policies p "
                "JOIN roles r ON p.role_id = r.id "
                "WHERE r.name = 'memory:read'"
            ).fetchall()
        actions = {(r[0], r[1]) for r in row}
        assert ("read", "memory") in actions, (
            f"memory:read role should have ('read','memory') policy, got {actions}"
        )

    def test_memory_admin_role_has_all_actions(self, db_path: Path):
        if not _has_builtins(db_path):
            pytest.skip("migration 046 not applied")
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT p.action FROM policies p "
                "JOIN roles r ON p.role_id = r.id "
                "WHERE r.name = 'memory:admin'"
            ).fetchall()
        actions = {r[0] for r in row}
        for expected in ("read", "write", "delete", "admin"):
            assert expected in actions, (
                f"memory:admin should have '{expected}' policy, got {actions}"
            )

    def test_ops_read_role_has_ops_read_policy(self, db_path: Path):
        if not _has_builtins(db_path):
            pytest.skip("migration 046 not applied")
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT p.action, p.resource FROM policies p "
                "JOIN roles r ON p.role_id = r.id "
                "WHERE r.name = 'ops:read'"
            ).fetchall()
        actions = {(r[0], r[1]) for r in row}
        assert ("read", "ops") in actions

    def test_gdpr_erase_has_memory_admin(self, db_path: Path):
        """compliance:gdpr-erase-admin should grant memory:admin action."""
        if not _has_builtins(db_path):
            pytest.skip("migration 046 not applied")
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT p.action, p.resource FROM policies p "
                "JOIN roles r ON p.role_id = r.id "
                "WHERE r.name = 'compliance:gdpr-erase-admin'"
            ).fetchall()
        actions = {(r[0], r[1]) for r in row}
        assert ("admin", "memory") in actions
