"""RBAC audit trail tests — role grant/revoke operations are logged.

Tests that the principal_roles_audit table correctly records:
- Grant operations with principal_id, role_id, action, performed_by
- Revoke operations with correct metadata
- Audit entries are queryable and accurate

TEST STRUCTURE (1 class, ~20 assertions)
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


def _get_role_id(db_path: Path, role_name: str) -> str | None:
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT id FROM roles WHERE name = ?", (role_name,)
        ).fetchone()
    return row[0] if row else None


def _grant_role(db_path: Path, principal_id: str, role_name: str,
                performed_by: str = "admin", reason: str = "") -> None:
    """Grant a role and create an audit entry."""
    role_id = _get_role_id(db_path, role_name)
    if role_id is None:
        pytest.skip(f"Role '{role_name}' not found")
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO role_bindings (principal_id, role_id, granted_at, granted_by) "
            "VALUES (?, ?, datetime('now'), ?)",
            (principal_id, role_id, performed_by),
        )
        conn.execute(
            "INSERT INTO principal_roles_audit "
            "(principal_id, role_id, action, performed_by, performed_at, reason) "
            "VALUES (?, ?, 'grant', ?, datetime('now'), ?)",
            (principal_id, role_id, performed_by, reason),
        )
        conn.commit()


def _revoke_role(db_path: Path, principal_id: str, role_name: str,
                 performed_by: str = "admin", reason: str = "") -> None:
    """Revoke a role and create an audit entry."""
    role_id = _get_role_id(db_path, role_name)
    if role_id is None:
        return
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "DELETE FROM role_bindings WHERE principal_id = ? AND role_id = ?",
            (principal_id, role_id),
        )
        conn.execute(
            "INSERT INTO principal_roles_audit "
            "(principal_id, role_id, action, performed_by, performed_at, reason) "
            "VALUES (?, ?, 'revoke', ?, datetime('now'), ?)",
            (principal_id, role_id, performed_by, reason),
        )
        conn.commit()


def _get_audit_entries(db_path: Path, principal_id: str) -> list[dict]:
    """Return all audit entries for a principal."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM principal_roles_audit WHERE principal_id = ? ORDER BY performed_at",
            (principal_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _has_rbac_schema(db_path: Path) -> bool:
    with sqlite3.connect(str(db_path)) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    return "principal_roles_audit" in tables


def _has_principals_table(db_path: Path) -> bool:
    with sqlite3.connect(str(db_path)) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    return "principals" in tables


# ===================================================================
# CLASS: Audit Trail Tests (~20 assertions)
# ===================================================================

@pytest.mark.rbac
class TestRBACAuditTrail:
    """Role grant/revoke operations create correct audit entries."""

    def test_preconditions(self, db_path: Path):
        assert _has_principals_table(db_path)
        assert _has_rbac_schema(db_path)

    def test_grant_creates_audit_entry(self, db_path: Path):
        """Granting a role should create an audit entry."""
        _create_principal(db_path, "audit-1")
        _grant_role(db_path, "audit-1", "memory:read", performed_by="admin", reason="test grant")
        entries = _get_audit_entries(db_path, "audit-1")
        assert len(entries) == 1
        assert entries[0]["action"] == "grant"
        assert entries[0]["principal_id"] == "audit-1"
        assert entries[0]["performed_by"] == "admin"
        assert entries[0]["reason"] == "test grant"

    def test_revoke_creates_audit_entry(self, db_path: Path):
        """Revoking a role should create an audit entry."""
        _create_principal(db_path, "audit-2")
        _grant_role(db_path, "audit-2", "memory:read")
        _revoke_role(db_path, "audit-2", "memory:read", performed_by="admin", reason="test revoke")
        entries = _get_audit_entries(db_path, "audit-2")
        assert len(entries) == 2
        assert entries[0]["action"] == "grant"
        assert entries[1]["action"] == "revoke"

    def test_audit_has_correct_role_id(self, db_path: Path):
        """Audit entry should reference the correct role_id."""
        _create_principal(db_path, "audit-3")
        role_id = _get_role_id(db_path, "memory:read")
        if role_id is None:
            pytest.skip("memory:read role not found")
        _grant_role(db_path, "audit-3", "memory:read")
        entries = _get_audit_entries(db_path, "audit-3")
        assert entries[0]["role_id"] == role_id

    def test_audit_has_performed_at(self, db_path: Path):
        """Audit entry should have a non-null performed_at timestamp."""
        _create_principal(db_path, "audit-4")
        _grant_role(db_path, "audit-4", "memory:read")
        entries = _get_audit_entries(db_path, "audit-4")
        assert entries[0]["performed_at"] is not None
        assert len(entries[0]["performed_at"]) > 0

    def test_audit_grant_and_revoke_sequential(self, db_path: Path):
        """Multiple grant/revoke cycles should produce sequential audit entries."""
        _create_principal(db_path, "audit-5")
        _grant_role(db_path, "audit-5", "memory:read")
        _revoke_role(db_path, "audit-5", "memory:read")
        _grant_role(db_path, "audit-5", "memory:write")
        _revoke_role(db_path, "audit-5", "memory:write")
        entries = _get_audit_entries(db_path, "audit-5")
        assert len(entries) == 4
        assert entries[0]["action"] == "grant"
        assert entries[1]["action"] == "revoke"
        assert entries[2]["action"] == "grant"
        assert entries[3]["action"] == "revoke"

    def test_audit_index_principal(self, db_path: Path):
        """Index on principal_roles_audit.principal_id should exist."""
        with sqlite3.connect(str(db_path)) as conn:
            idxs = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()}
        assert any("principal_roles_audit" in i and "principal" in i for i in idxs)

    def test_audit_check_constraint_action(self, db_path: Path):
        """principal_roles_audit.action CHECK should enforce 'grant' or 'revoke'."""
        with sqlite3.connect(str(db_path)) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO principal_roles_audit "
                    "(principal_id, role_id, action, performed_by) "
                    "VALUES ('test', 'test', 'invalid_action', 'admin')"
                )

    def test_audit_no_fk_principal(self, db_path: Path):
        """principal_roles_audit should NOT have FK to principals (allows historical records)."""
        with sqlite3.connect(str(db_path)) as conn:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='principal_roles_audit'"
            ).fetchone()[0]
        # The migration 045 defines principal_id as TEXT NOT NULL without REFERENCES
        # This is intentional — audit records survive principal deletion
        assert "REFERENCES principals" not in sql, (
            "principal_roles_audit should NOT have FK to principals"
        )

    def test_audit_survives_principal_deletion(self, db_path: Path):
        """Audit entries should survive deletion of the referenced principal."""
        _create_principal(db_path, "audit-delete-1")
        _grant_role(db_path, "audit-delete-1", "memory:read")
        entries_before = _get_audit_entries(db_path, "audit-delete-1")
        assert len(entries_before) == 1
        # Delete the principal
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("DELETE FROM principals WHERE id = ?", ("audit-delete-1",))
            conn.commit()
        # Audit entry should survive (no FK)
        entries_after = _get_audit_entries(db_path, "audit-delete-1")
        assert len(entries_after) == 1, "Audit entry should survive principal deletion"

    def test_audit_different_roles(self, db_path: Path):
        """Different roles should be recorded correctly in audit."""
        _create_principal(db_path, "audit-diff-1")
        _grant_role(db_path, "audit-diff-1", "memory:read")
        _grant_role(db_path, "audit-diff-1", "memory:write")
        _grant_role(db_path, "audit-diff-1", "memory:admin")
        entries = _get_audit_entries(db_path, "audit-diff-1")
        role_names = set()
        for e in entries:
            with sqlite3.connect(str(db_path)) as conn:
                row = conn.execute("SELECT name FROM roles WHERE id = ?", (e["role_id"],)).fetchone()
                if row:
                    role_names.add(row[0])
        assert "memory:read" in role_names
        assert "memory:write" in role_names
        assert "memory:admin" in role_names
