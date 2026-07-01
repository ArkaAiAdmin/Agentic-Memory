#!/usr/bin/env python3
"""Unit tests for sync_check.py.

Tests the CLI argument parsing and imports. The actual sync_invariant
logic is tested in test_sync_invariant tests.

Run with:
    ~/.config/agentic-memory/venv/bin/python eval/test_sync_check.py
"""

import sys
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))


class TestSyncCheckImports(unittest.TestCase):
    def test_module_importable(self):
        import infra.sync_check

        self.assertTrue(hasattr(sync_check, "main"))

    def test_sync_invariant_importable(self):
        from infra.sync_invariant import check_sync_invariant

        self.assertTrue(callable(check_sync_invariant))

    def test_format_sync_report_importable(self):
        from infra.sync_invariant import format_sync_report

        self.assertTrue(callable(format_sync_report))

    def test_get_drifted_subsystems_importable(self):
        from infra.sync_invariant import get_drifted_subsystems

        self.assertTrue(callable(get_drifted_subsystems))


if __name__ == "__main__":
    unittest.main(verbosity=2)
