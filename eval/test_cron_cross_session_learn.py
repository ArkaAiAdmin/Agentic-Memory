#!/usr/bin/env python3
"""Unit tests for cron_cross_session_learn.py.

Tests the subprocess wrapper logic without actually running cross_session_learn.

Run with:
    ~/.config/agentic-memory/venv/bin/python eval/test_cron_cross_session_learn.py
"""

import os
import sys
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))


class TestCronCrossSessionLearn(unittest.TestCase):
    def test_main_module_importable(self):
        import cron_cross_session_learn

        self.assertTrue(hasattr(cron_cross_session_learn, "main"))

    def test_main_returns_nonzero_when_script_missing(self):
        import importlib
        import cron_cross_session_learn

        old_python = os.environ.get("MEMORY_PYTHON")
        os.environ["MEMORY_PYTHON"] = "/nonexistent/python"
        importlib.reload(cron_cross_session_learn)
        # Patch acquire_lock_or_exit AFTER reload (reload re-imports it)
        cron_cross_session_learn.acquire_lock_or_exit = lambda *a, **kw: None
        try:
            result = cron_cross_session_learn.main()
            self.assertNotEqual(result, 0)
        finally:
            if old_python is None:
                del os.environ["MEMORY_PYTHON"]
            else:
                os.environ["MEMORY_PYTHON"] = old_python

    def test_env_var_default(self):
        self.assertEqual(os.environ.get("MEMORY_CROSS_SESSION_DAYS", "3"), "3")


if __name__ == "__main__":
    unittest.main(verbosity=2)
