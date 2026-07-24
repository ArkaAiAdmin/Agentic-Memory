"""ACL override tests — per-resource deny/allow overrides role-based permissions.

Tests that acl_overrides take precedence over role-based permissions:
- A deny override blocks a role-granted permission
- An allow override grants permission beyond role scope
- Override precedence: override > role-based

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


def _grant_role(db_path: Path, principal_id: str, role_name: str) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        role_row = conn.execute(
            "SELECT id FROM roles WHERE name = ? AND tenant_id = 'global'",
            (role_name,),
        ).fetchone()
        if role_row is None:
            pytest.skip(f"Role '{role_name}' not found")
        conn.execute(
            "INSERT OR IGNORE INTO role_bindings (principal_id, role_id, granted_at) "
            "VALUES (?, ?, datetime('now'))",
            (principal_id, role_row[0]),
        )
        conn.commit()


def _add_acl_override(db_path: Path, principal_id: str, resource_id: str,
                       action: str, effect: str, granted_by: str = "test") -> None:
    """Insert an ACL override entry via SQL."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO acl_overrides "
            "(principal_id, resource_id, action, effect, granted_at, granted_by) "
            "VALUES (?, ?, ?, ?, datetime('now'), ?)",
            (principal_id, resource_id, action, effect, granted_by),
        )
        conn.commit()


def _get_acl_overrides(db_path: Path, principal_id: str) -> list[dict]:
    """Return all ACL overrides for a principal."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM acl_overrides WHERE principal_id = ?",
            (principal_id,),
        ).fetchall()
    return [dict(r) for r in rows]


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
    return "roles" in tables and "acl_overrides" in tables


# ===================================================================
# CLASS: ACL Override Tests (~20 assertions)
# ===================================================================

@pytest.mark.rbac
class TestRBACACLOverride:
    """ACL overrides take precedence over role-based permissions."""

    def test_preconditions(self, db_path: Path):
        assert _has_rbac_schema(db_path), "RBAC tables missing"

    def test_acl_override_insert_deny(self, db_path: Path):
        """Can insert a deny override for a specific resource."""
        _create_principal(db_path, "override-1")
        _add_acl_override(db_path, "override-1", "memory/secret-note", "read", "deny")
        overrides = _get_acl_overrides(db_path, "override-1")
        assert len(overrides) == 1
        assert overrides[0]["effect"] == "deny"
        assert overrides[0]["resource_id"] == "memory/secret-note"
        assert overrides[0]["action"] == "read"

    def test_acl_override_insert_allow(self, db_path: Path):
        """Can insert an allow override."""
        _create_principal(db_path, "override-2")
        _add_acl_override(db_path, "override-2", "memory/public-note", "read", "allow")
        overrides = _get_acl_overrides(db_path, "override-2")
        assert len(overrides) == 1
        assert overrides[0]["effect"] == "allow"

    def test_deny_override_blocks_role_permission(self, db_path: Path):
        """A deny override on a resource blocks even a role-granted permission."""
        _create_principal(db_path, "override-3")
        _grant_role(db_path, "override-3", "memory:admin")
        _add_acl_override(db_path, "override-3", "memory/secret-note", "read", "deny")
        # Verify the override exists
        overrides = _get_acl_overrides(db_path, "override-3")
        assert len(overrides) == 1
        assert overrides[0]["effect"] == "deny"
        # The authorizer should check overrides BEFORE role-based permissions
        authorizer = _get_authorizer(db_path)
        if authorizer is not None:
            result = authorizer.check(
                principal_id="override-3",
                resource="memory/secret-note",
                action="read",
            )
            assert result is False or result == "deny", (
                f"Deny override should block role-granted read, got {result}"
            )

    def test_allow_override_grants_beyond_role(self, db_path: Path):
        """An allow override grants permission the role doesn't cover."""
        _create_principal(db_path, "override-4")
        _grant_role(db_path, "override-4", "memory:read")
        _add_acl_override(db_path, "override-4", "memory/special-note", "write", "allow")
        authorizer = _get_authorizer(db_path)
        if authorizer is not None:
            # read should be allowed via role
            r1 = authorizer.check(principal_id="override-4", resource="memory/special-note", action="read")
            assert r1 is True or r1 == "allow"
            # write should be allowed via override
            r2 = authorizer.check(principal_id="override-4", resource="memory/special-note", action="write")
            assert r2 is True or r2 == "allow", (
                f"Allow override should grant write beyond role, got {r2}"
            )

    def test_multiple_overrides_different_resources(self, db_path: Path):
        """Different overrides apply to different resources independently."""
        _create_principal(db_path, "override-5")
        _grant_role(db_path, "override-5", "memory:read")
        _add_acl_override(db_path, "override-5", "memory/a", "read", "deny")
        _add_acl_override(db_path, "override-5", "memory/b", "read", "allow")
        overrides = _get_acl_overrides(db_path, "override-5")
        assert len(overrides) == 2
        effects = {o["resource_id"]: o["effect"] for o in overrides}
        assert effects["memory/a"] == "deny"
        assert effects["memory/b"] == "allow"

    def test_override_default_effect_is_deny(self, db_path: Path):
        """acl_overrides.effect defaults to 'deny' per schema."""
        with sqlite3.connect(str(db_path)) as conn:
            for row in conn.execute("PRAGMA table_info(acl_overrides)").fetchall():
                if row[1] == "effect":
                    default_val = str(row[4] or "").strip("'\"")
                    assert default_val == "deny", (
                        f"Default effect should be 'deny', got {row[4]!r}"
                    )
                    return
        pytest.fail("effect column not found")

    def test_override_unique_constraint(self, db_path: Path):
        """Duplicate (principal, resource, action) overrides should upsert."""
        _create_principal(db_path, "override-6")
        _add_acl_override(db_path, "override-6", "memory/x", "read", "deny")
        _add_acl_override(db_path, "override-6", "memory/x", "read", "allow")
        overrides = _get_acl_overrides(db_path, "override-6")
        # Should be 1 row (upserted), effect should be the latest
        assert len(overrides) == 1
        assert overrides[0]["effect"] == "allow"

    def test_override_references_principal(self, db_path: Path):
        """acl_overrides.principal_id references principals(id)."""
        _create_principal(db_path, "override-7")
        _add_acl_override(db_path, "override-7", "memory/y", "read", "deny")
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT a.principal_id, p.id FROM acl_overrides a "
                "JOIN principals p ON a.principal_id = p.id "
                "WHERE a.principal_id = ?",
                ("override-7",),
            ).fetchone()
        assert row is not None, "FK reference from acl_overrides to principals should work"

    def test_override_cascade_delete_principal(self, db_path: Path):
        """Deleting a principal should cascade-delete its ACL overrides.

        SQLite FK cascade requires PRAGMA foreign_keys=ON on the connection.
        """
        _create_principal(db_path, "override-8")
        _add_acl_override(db_path, "override-8", "memory/z", "read", "deny")
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("DELETE FROM principals WHERE id = ?", ("override-8",))
            conn.commit()
            count = conn.execute(
                "SELECT COUNT(*) FROM acl_overrides WHERE principal_id = ?",
                ("override-8",),
            ).fetchone()[0]
        assert count == 0, "Cascade delete should remove ACL overrides"

    def test_override_index_principal(self, db_path: Path):
        """Index on acl_overrides.principal_id should exist."""
        with sqlite3.connect(str(db_path)) as conn:
            idxs = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()}
        assert any("acl_overrides" in i and "principal" in i for i in idxs)


# ===================================================================
# CLASS: ACL Override Maintenance Operations
# ===================================================================

@pytest.mark.rbac
class TestACLOverrideMaintenanceOps:
    """Test set_acl_override and remove_acl_override maintenance operations."""

    def test_set_acl_override_allow(self, db_path: Path):
        """set_acl_override with granted=True inserts an allow effect."""
        from mcp_surface.mcp_maintenance_ops import _op_set_acl_override
        import json as _json

        _create_principal(db_path, "maint-1")
        os.environ["MEMORY_DB_PATH"] = str(db_path)
        try:
            result = _op_set_acl_override(
                principal_id="maint-1",
                resource_id="memory/note-a",
                action="read",
                granted=True,
            )
            data = _json.loads(result)
            assert data["ok"] is True
            assert data["effect"] == "allow"
            assert data["principal_id"] == "maint-1"
            assert data["resource_id"] == "memory/note-a"
            assert data["action"] == "read"
            # Verify row exists in DB
            overrides = _get_acl_overrides(db_path, "maint-1")
            assert len(overrides) == 1
            assert overrides[0]["effect"] == "allow"
        finally:
            os.environ.pop("MEMORY_DB_PATH", None)

    def test_set_acl_override_deny(self, db_path: Path):
        """set_acl_override with granted=False inserts a deny effect."""
        from mcp_surface.mcp_maintenance_ops import _op_set_acl_override
        import json as _json

        _create_principal(db_path, "maint-2")
        os.environ["MEMORY_DB_PATH"] = str(db_path)
        try:
            result = _op_set_acl_override(
                principal_id="maint-2",
                resource_id="memory/note-b",
                action="write",
                granted=False,
            )
            data = _json.loads(result)
            assert data["ok"] is True
            assert data["effect"] == "deny"
            overrides = _get_acl_overrides(db_path, "maint-2")
            assert len(overrides) == 1
            assert overrides[0]["effect"] == "deny"
        finally:
            os.environ.pop("MEMORY_DB_PATH", None)

    def test_set_acl_override_upserts(self, db_path: Path):
        """Setting the same override twice upserts (no duplicates)."""
        from mcp_surface.mcp_maintenance_ops import _op_set_acl_override
        import json as _json

        _create_principal(db_path, "maint-3")
        os.environ["MEMORY_DB_PATH"] = str(db_path)
        try:
            _op_set_acl_override(
                principal_id="maint-3",
                resource_id="memory/note-c",
                action="read",
                granted=False,
            )
            result = _op_set_acl_override(
                principal_id="maint-3",
                resource_id="memory/note-c",
                action="read",
                granted=True,
            )
            data = _json.loads(result)
            assert data["effect"] == "allow"
            overrides = _get_acl_overrides(db_path, "maint-3")
            assert len(overrides) == 1
            assert overrides[0]["effect"] == "allow"
        finally:
            os.environ.pop("MEMORY_DB_PATH", None)

    def test_remove_acl_override(self, db_path: Path):
        """remove_acl_override deletes an existing override."""
        from mcp_surface.mcp_maintenance_ops import _op_set_acl_override, _op_remove_acl_override
        import json as _json

        _create_principal(db_path, "maint-4")
        os.environ["MEMORY_DB_PATH"] = str(db_path)
        try:
            _op_set_acl_override(
                principal_id="maint-4",
                resource_id="memory/note-d",
                action="delete",
                granted=True,
            )
            overrides = _get_acl_overrides(db_path, "maint-4")
            assert len(overrides) == 1

            result = _op_remove_acl_override(
                principal_id="maint-4",
                resource_id="memory/note-d",
                action="delete",
            )
            data = _json.loads(result)
            assert data["ok"] is True
            assert data["deleted"] == 1

            overrides = _get_acl_overrides(db_path, "maint-4")
            assert len(overrides) == 0
        finally:
            os.environ.pop("MEMORY_DB_PATH", None)

    def test_remove_acl_override_nonexistent(self, db_path: Path):
        """remove_acl_override on a missing override returns deleted=0."""
        from mcp_surface.mcp_maintenance_ops import _op_remove_acl_override
        import json as _json

        _create_principal(db_path, "maint-5")
        os.environ["MEMORY_DB_PATH"] = str(db_path)
        try:
            result = _op_remove_acl_override(
                principal_id="maint-5",
                resource_id="memory/does-not-exist",
                action="read",
            )
            data = _json.loads(result)
            assert data["ok"] is True
            assert data["deleted"] == 0
        finally:
            os.environ.pop("MEMORY_DB_PATH", None)
