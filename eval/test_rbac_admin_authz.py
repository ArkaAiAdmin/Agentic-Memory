#!/usr/bin/env python3
"""CHANGE 4 — RBAC admin endpoints must enforce authorization.

The mutating RBAC/ACL handlers now call ``APIServer._require_rbac_admin``,
which denies non-admin principals (403) and admits principals holding the
memory/ops super-admin role.

This test verifies the GATE wiring directly (the CHANGE 4 deliverable) by
driving the handlers with a faked request object and a controlled
``mcp_authorize`` result, so it does not depend on the RBAC engine's
DB-resolution plumbing.

Run:
    ~/.config/agentic-memory/venv/bin/python -m unittest eval.test_rbac_admin_authz -v
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from infra.api_server import APIRequestHandler


class _FakeHandler:
    """Minimal stand-in for the APIServer request handler surface used by
    ``_require_rbac_admin`` and the RBAC mutating handlers."""

    # Pull in the real gate implementation under test.
    _require_rbac_admin = APIRequestHandler._require_rbac_admin

    def __init__(self, principal_id, tenant_id="default", db_path=None):
        self._principal_id = principal_id
        self._principal = type("_Principal", (), {"id": principal_id, "tenant_id": tenant_id})()
        self._status = None
        self._body = None
        self.server = type("_Server", (), {"db_path": db_path or Path("/tmp/none.db")})()

    def _error(self, msg, code):
        self._status = code
        self._body = msg
        return False

    def _write_json(self, obj, code=200):
        self._status = code
        self._body = obj
        return True


class TestRBACAdminAuthzGate(unittest.TestCase):
    def _make_handler(self, principal_id, authorize_result):
        h = _FakeHandler(principal_id)
        # mcp_authorize is imported lazily inside the gate; patch the
        # canonical source so the gate's `from infra.authorizer import
        # mcp_authorize` picks up the fake.
        patcher = mock.patch(
            "infra.authorizer.mcp_authorize",
            return_value=authorize_result,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return h

    def test_non_admin_is_forbidden(self):
        h = self._make_handler("nonadmin", authorize_result=False)
        ok = h._require_rbac_admin()
        self.assertFalse(ok)
        self.assertEqual(h._status, 403)

    def test_admin_is_allowed(self):
        h = self._make_handler("admin", authorize_result=True)
        ok = h._require_rbac_admin()
        self.assertTrue(ok)
        self.assertIsNone(h._status)

    def test_unavailable_authorizer_fails_closed(self):
        h = _FakeHandler("anyone")
        patcher = mock.patch(
            "infra.authorizer.mcp_authorize",
            side_effect=ImportError("authorizer missing"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        ok = h._require_rbac_admin()
        self.assertFalse(ok)
        self.assertEqual(h._status, 403)


if __name__ == "__main__":
    unittest.main()
