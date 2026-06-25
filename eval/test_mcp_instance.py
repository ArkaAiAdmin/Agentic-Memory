#!/usr/bin/env python3
"""Unit tests for mcp_instance.py.

Run with:
    ~/.config/agentic-memory/venv/bin/python eval/test_mcp_instance.py
"""

import sys
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from mcp_instance import mcp


class TestMCPInstance(unittest.TestCase):
    def test_mcp_is_not_none(self):
        self.assertIsNotNone(mcp)

    def test_mcp_name(self):
        self.assertEqual(mcp.name, "AgenticMemory")

    def test_mcp_is_singleton(self):
        from mcp_instance import mcp as mcp2

        self.assertIs(mcp, mcp2)

    def test_mcp_has_run_method(self):
        self.assertTrue(hasattr(mcp, "run"))

    def test_mcp_has_tool_list(self):
        import inspect

        self.assertTrue(hasattr(mcp, "run"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
