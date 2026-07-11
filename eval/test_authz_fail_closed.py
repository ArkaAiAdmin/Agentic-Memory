"""Authorization hardening tests (GAPs 1, 4, 5).

Verifies:
  - mcp_authorize is FAIL-CLOSED by default (MEMORY_AUTH_MODE=closed) and
    only allows the legacy behavior when explicitly set to "open".
  - Tenant scoping: a principal may only act on its own tenant unless it
    holds a cross-tenant admin role.
  - The REST GDPR-erase handler resolves tenant_id from the authenticated
    principal and IGNORES any tenant_id supplied in the request body
    (no cross-tenant escalation via body injection).
"""

import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.db import open_db
from infra.migration_runner import run_migrations
from infra.rbac import Principal, seed_default_roles, grant_role
from infra import authorizer
from infra.api_server import APIRequestHandler


@pytest.fixture
def db():
    p = Path(tempfile.mktemp(suffix=".db"))
    with open_db(p, timeout=10) as conn:
        run_migrations(conn)
        seed_default_roles(conn)
        conn.commit()
    yield str(p)
    p.unlink(missing_ok=True)


def _insert_principal(conn, pid, tenant):
    conn.execute(
        "INSERT INTO principals (id, kind, tenant_id) VALUES (?, 'user', ?)",
        (pid, tenant),
    )


def test_fail_closed_denies_unauthenticated(db, monkeypatch):
    monkeypatch.setenv("MEMORY_AUTH_MODE", "closed")
    assert authorizer.mcp_authorize(None, "read", "memory", db_path=db) is False


def test_open_allows_unauthenticated(db, monkeypatch):
    monkeypatch.setenv("MEMORY_AUTH_MODE", "open")
    assert authorizer.mcp_authorize(None, "read", "memory", db_path=db) is True


def test_role_grant_allows(db, monkeypatch):
    monkeypatch.setenv("MEMORY_AUTH_MODE", "closed")
    with open_db(db, timeout=5) as conn:
        _insert_principal(conn, "acme-reader", "acme")
        grant_role(conn, "acme-reader", "role:memory:read:default")
    assert authorizer.mcp_authorize("acme-reader", "read", "memory", db_path=db) is True


def test_tenant_scope_denies_cross_tenant(db, monkeypatch):
    monkeypatch.setenv("MEMORY_AUTH_MODE", "closed")
    with open_db(db, timeout=5) as conn:
        _insert_principal(conn, "acme-writer", "acme")
        grant_role(conn, "acme-writer", "role:memory:write:default")
    # Same tenant -> allowed.
    assert (
        authorizer.mcp_authorize("acme-writer", "write", "memory", db_path=db, tenant_id="acme")
        is True
    )
    # Mismatched tenant, not a cross-tenant admin -> denied.
    assert (
        authorizer.mcp_authorize("acme-writer", "write", "memory", db_path=db, tenant_id="globex")
        is False
    )


def test_gdpr_erase_ignores_body_tenant(monkeypatch):
    """REST handler must derive tenant from the principal, not the body."""
    monkeypatch.setenv("MEMORY_AUTH_MODE", "closed")
    captured = {}

    def fake_authorize(**kwargs):
        captured["auth"] = kwargs
        return True

    def fake_erase(conn, principal_id, data_subject_sub, tenant_id):
        captured["erase_tenant"] = tenant_id
        return {"status": "completed"}

    monkeypatch.setattr(authorizer, "mcp_authorize", fake_authorize)
    monkeypatch.setattr("infra.gdpr.gdpr_erase", fake_erase)

    class FakeHandler:
        def __init__(self):
            self._principal = Principal(id="acme-admin", kind="user", tenant_id="acme")
            self._principal_id = "acme-admin"
            self.server = types.SimpleNamespace(
                db_path=str(Path(tempfile.mktemp(suffix=".db")))
            )
            self._req = {"data_subject_sub": "x@y.com", "tenant_id": "globex"}
            self.result = None

        def _read_json_body(self):
            return self._req

        def _write_json(self, data, code=200):
            self.result = (code, data)

        def _error(self, msg, code):
            self.result = (code, {"error": msg})

    h = FakeHandler()
    APIRequestHandler._handle_gdpr_erase(h)
    assert captured["erase_tenant"] == "acme", (
        f"tenant must come from principal, not body: {captured}"
    )
    assert h.result[0] == 200
