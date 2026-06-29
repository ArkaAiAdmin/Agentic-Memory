#!/usr/bin/env python3
"""Unit tests for cron_crdt_sync.py.

Tests the config loading, peer validation, and error paths.
The actual sync with remote peers requires a running server — tested in e2e.

Run with:
    ~/.config/agentic-memory/venv/bin/python eval/test_cron_crdt_sync.py
"""

import os
import sys
import unittest
from pathlib import Path

# 2026-06-29 fix: resolve from the test file location, not the user's home
# dir. On CI runners the ~/.config/agentic-memory install dir does not exist.
INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))


class TestCronCrdtSyncImports(unittest.TestCase):
    def test_module_importable(self):
        import cron_crdt_sync

        self.assertTrue(hasattr(cron_crdt_sync, "main"))

    def test_main_returns_1_when_no_db(self):
        from cron_crdt_sync import main

        old_env = os.environ.get("MEMORY_DB_PATH")
        os.environ["MEMORY_DB_PATH"] = "/nonexistent/memory.db"
        try:
            result = main()
            self.assertEqual(result, 1)
        finally:
            if old_env is None:
                del os.environ["MEMORY_DB_PATH"]
            else:
                os.environ["MEMORY_DB_PATH"] = old_env

    def test_env_vars_set_on_import(self):
        self.assertEqual(os.environ.get("MEMORY_MULTI_AGENT"), "1")
        self.assertEqual(os.environ.get("MEMORY_CRDT_ENABLED", "0"), "0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
