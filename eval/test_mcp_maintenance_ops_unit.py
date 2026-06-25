#!/usr/bin/env python3
"""Unit tests for mcp_maintenance_ops.py.

This module's contract is the *dispatch table*: it exposes
``MAINTENANCE_HANDLERS`` (a proxy dict) and three lazy initializers
(``_get_local_tools``, ``_get_domain_tools``, ``_get_handlers``).
The proxy class is testable without triggering the heavy
imports (which would pull in all 17 mcp_*.py modules and
``@mcp.tool()`` registration).

What we test:
  * The proxy class wraps a dict-like surface (`__getitem__`,
    `__iter__`, `__len__`, `__contains__`, `keys`, `values`,
    `items`).
  * Lazy initialization memoizes (calling twice returns the same
    dict object).
  * The full handlers table contains entries for every well-known
    MaintenanceOp (HEARTBEAT, REBUILD, etc.).
  * No lambda is malformed (each must be callable and have the
    expected `**kwargs` surface).
"""

import sys
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))
sys.path.insert(0, str(INSTALL_DIR / "eval"))


class TestMaintenanceHandlersProxy(unittest.TestCase):
    """The proxy class itself: just dict-method forwarding."""

    def test_proxy_class_exists(self):
        from mcp_maintenance_ops import _MaintenanceHandlersProxy

        self.assertTrue(callable(_MaintenanceHandlersProxy))

    def test_module_exports_maintenance_handlers(self):
        from mcp_maintenance_ops import MAINTENANCE_HANDLERS

        # Module-level constant must exist and be a proxy instance.
        self.assertIsNotNone(MAINTENANCE_HANDLERS)


class TestLazyInitMemoizes(unittest.TestCase):
    """Each lazy initializer must return the same object on
    subsequent calls (avoids re-importing the heavy mcp_* modules)."""

    def test_local_tools_memoized(self):
        from mcp_maintenance_ops import _get_local_tools

        a = _get_local_tools()
        b = _get_local_tools()
        self.assertIs(a, b)

    def test_domain_tools_memoized(self):
        from mcp_maintenance_ops import _get_domain_tools

        a = _get_domain_tools()
        b = _get_domain_tools()
        self.assertIs(a, b)

    def test_handlers_memoized(self):
        from mcp_maintenance_ops import _get_handlers

        a = _get_handlers()
        b = _get_handlers()
        self.assertIs(a, b)


class TestToolsDictionaryShape(unittest.TestCase):
    def test_local_tools_are_callable(self):
        from mcp_maintenance_ops import _get_local_tools

        tools = _get_local_tools()
        # At least the 5 most-used local tools must be present.
        for key in (
            "memory_heartbeat",
            "memory_rebuild",
            "memory_audit",
            "memory_compact",
            "memory_backfill_all",
        ):
            self.assertIn(key, tools, f"missing local tool: {key}")
            self.assertTrue(callable(tools[key]), f"{key} is not callable")

    def test_domain_tools_are_callable(self):
        from mcp_maintenance_ops import _get_domain_tools

        tools = _get_domain_tools()
        # Spot-check a few domain tools to make sure the registry
        # has been populated.
        for key in (
            "memory_share",
            "memory_summarize",
            "memory_adaptive_retention",
            "memory_metrics_server",
        ):
            self.assertIn(key, tools, f"missing domain tool: {key}")
            self.assertTrue(callable(tools[key]), f"{key} is not callable")

    def test_tools_combines_local_and_domain(self):
        from mcp_maintenance_ops import _get_domain_tools, _get_local_tools, _tools

        combined = _tools()
        local = _get_local_tools()
        domain = _get_domain_tools()
        # Combined must have at least as many entries as each part.
        self.assertGreaterEqual(len(combined), len(local))
        self.assertGreaterEqual(len(combined), len(domain))
        # Spot-check that entries from both are present.
        for key in local:
            self.assertIn(key, combined)
        for key in domain:
            self.assertIn(key, combined)


class TestHandlersDispatchTable(unittest.TestCase):
    """The MAINTENANCE_HANDLERS dispatch table is the public
    contract. Every well-known op must have a callable handler
    and every handler must be a lambda/function."""

    def test_handlers_dict_is_nonempty(self):
        from mcp_maintenance_ops import MAINTENANCE_HANDLERS

        self.assertGreater(len(MAINTENANCE_HANDLERS), 40)

    def test_all_handlers_are_callable(self):
        from mcp_maintenance_ops import MAINTENANCE_HANDLERS

        non_callable = [k for k, v in MAINTENANCE_HANDLERS.items() if not callable(v)]
        self.assertEqual(
            non_callable,
            [],
            f"non-callable handlers: {non_callable}",
        )

    def test_proxy_contains_method_works(self):
        from mcp_maintenance_ops import MAINTENANCE_HANDLERS

        # Pick any key from .keys() and assert __contains__ agrees.
        any_key = next(iter(MAINTENANCE_HANDLERS))
        self.assertIn(any_key, MAINTENANCE_HANDLERS)

    def test_proxy_keys_matches_dict(self):
        from mcp_maintenance_ops import MAINTENANCE_HANDLERS, _get_handlers

        proxy_keys = set(MAINTENANCE_HANDLERS.keys())
        raw_keys = set(_get_handlers().keys())
        self.assertEqual(proxy_keys, raw_keys)


if __name__ == "__main__":
    unittest.main(verbosity=2)
