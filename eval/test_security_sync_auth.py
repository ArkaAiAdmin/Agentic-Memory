"""Regression tests for sync_server security fixes (Phase 2).

Covers:
- SEC-1 fix: _require_auth denies on non-loopback when SYNC_AUTH_TOKEN is unset
- SEC-1 fix: _require_auth allows loopback without token (local dev convenience)
- SEC-1 fix: _require_auth validates Bearer token when set
- Dead code: unreachable return True removed from _check_hmac
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))


class TestSyncServerAuthBypassFix(unittest.TestCase):
    """SEC-1 (2026-06-27): auth bypass when MEMORY_SYNC_TOKEN is unset."""

    def _make_handler(self, host="127.0.0.1", token_env="MEMORY_SYNC_TOKEN"):
        """Import fresh _SyncHandler and return an instance with the given host."""
        # We patch the environment before importing/loading the module, clearing both sync & api token
        with patch.dict(os.environ, {token_env: "", "MEMORY_API_TOKEN": ""}, clear=False):
            # Force reimport to pick up patched env
            for mod in list(sys.modules):
                if mod == "sync_server" or mod.startswith("sync_server.") or mod == "infra.sync_server" or mod.startswith("infra.sync_server."):
                    del sys.modules[mod]
            from infra.sync_server import _SyncHandler

            handler = _SyncHandler.__new__(_SyncHandler)
            handler.host = host
            handler.headers = {}
            return handler

    def test_loopback_denies_without_token_by_default(self):
        handler = self._make_handler(host="127.0.0.1")
        with patch.object(handler, "_error"):
            result = handler._require_auth()
        self.assertFalse(result)

    def test_loopback_allows_without_token_when_opted_out(self):
        with patch.dict(os.environ, {"MEMORY_SYNC_ALLOW_UNAUTHENTICATED_LOOPBACK": "1"}, clear=False):
            handler = self._make_handler(host="127.0.0.1")
            result = handler._require_auth()
            self.assertTrue(result)

    def test_loopback_ipv6_allows_without_token_when_opted_out(self):
        with patch.dict(os.environ, {"MEMORY_SYNC_ALLOW_UNAUTHENTICATED_LOOPBACK": "1"}, clear=False):
            handler = self._make_handler(host="::1")
            result = handler._require_auth()
            self.assertTrue(result)

    def test_non_loopback_denies_without_token(self):
        handler = self._make_handler(host="0.0.0.0")
        # _require_auth calls self._error which we need to suppress
        with patch.object(handler, "_error"):
            result = handler._require_auth()
        self.assertFalse(result)

    def test_non_loopback_hostname_denies_without_token(self):
        handler = self._make_handler(host="192.168.1.10")
        with patch.object(handler, "_error"):
            result = handler._require_auth()
        self.assertFalse(result)

    def test_non_loopback_with_token_allows(self):
        with patch.dict(os.environ, {"MEMORY_SYNC_TOKEN": "secret123"}, clear=False):
            for mod in list(sys.modules):
                if mod == "sync_server" or mod.startswith("sync_server.") or mod == "infra.sync_server" or mod.startswith("infra.sync_server."):
                    del sys.modules[mod]
            from infra.sync_server import _SyncHandler

            handler = _SyncHandler.__new__(_SyncHandler)
            handler.host = "0.0.0.0"
            handler.headers = {"Authorization": "Bearer secret123"}
            with patch.object(handler, "_error"):
                result = handler._require_auth()
            self.assertTrue(result)

    def test_bearer_token_mismatch_denies(self):
        with patch.dict(os.environ, {"MEMORY_SYNC_TOKEN": "correct_token"}, clear=False):
            for mod in list(sys.modules):
                if mod == "sync_server" or mod.startswith("sync_server.") or mod == "infra.sync_server" or mod.startswith("infra.sync_server."):
                    del sys.modules[mod]
            from infra.sync_server import _SyncHandler

            handler = _SyncHandler.__new__(_SyncHandler)
            handler.host = "127.0.0.1"
            handler.headers = {"Authorization": "Bearer wrong_token"}
            with patch.object(handler, "_error"):
                result = handler._require_auth()
            self.assertFalse(result)

    def test_missing_bearer_header_denies(self):
        with patch.dict(os.environ, {"MEMORY_SYNC_TOKEN": "secret123"}, clear=False):
            for mod in list(sys.modules):
                if mod == "sync_server" or mod.startswith("sync_server.") or mod == "infra.sync_server" or mod.startswith("infra.sync_server."):
                    del sys.modules[mod]
            from infra.sync_server import _SyncHandler

            handler = _SyncHandler.__new__(_SyncHandler)
            handler.host = "127.0.0.1"
            handler.headers = {}
            with patch.object(handler, "_error"):
                result = handler._require_auth()
            self.assertFalse(result)


class TestSyncServerHmacDeadCode(unittest.TestCase):
    """Verify _check_hmac has no unreachable return statements after the fix."""

    def test_check_hmac_no_double_return(self):
        """SEC-4 fix: unreachable return True removed from _check_hmac.

        The function should have exactly TWO `return True` statements,
        both in legitimate positions:
          - line 208: short-circuit when SYNC_HMAC_SECRET is empty
          - line 225: HMAC matched
        The original bug was a THIRD unreachable `return True` immediately
        after the hmac.compare_digest branch.
        """
        import inspect

        with patch.dict(os.environ, {"MEMORY_SYNC_TOKEN": "tok"}, clear=False):
            for mod in list(sys.modules):
                if mod == "sync_server" or mod.startswith("sync_server.") or mod == "infra.sync_server" or mod.startswith("infra.sync_server."):
                    del sys.modules[mod]
            from infra.sync_server import _SyncHandler

            source = inspect.getsource(_SyncHandler._check_hmac)
            returns = [line.strip() for line in source.splitlines() if "return True" in line]
            self.assertEqual(
                len(returns),
                2,
                f"_check_hmac should have exactly two `return True` (early-exit + happy-path), found: {returns}",
            )


if __name__ == "__main__":
    unittest.main()
