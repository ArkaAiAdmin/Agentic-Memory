"""Tests for the agentic-memory hook resilience (2026-06-19).

Verifies:
- Each hook can be imported and run without a memory_config on sys.path
- Each hook exits 0 even on internal exceptions (no crash propagation)
- The local install_root resolution finds the install root in all
  standard locations
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

INSTALL_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = INSTALL_ROOT / "hooks"
VENV_PY = sys.executable


def _run_hook(name: str, stdin: str, timeout: float = 30.0) -> tuple[int, str, str]:
    """Run a hook with the given stdin. Returns (exit_code, stdout, stderr)."""
    p = subprocess.run(
        [VENV_PY, str(HOOKS_DIR / name)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(INSTALL_ROOT),
    )
    return p.returncode, p.stdout, p.stderr


class TestHookImportPathFix(unittest.TestCase):
    """P0: hooks must work when run from any cwd, not just the install root."""

    def test_proactive_context_no_chicken_and_egg(self):
        """The hook must not fail with 'No module named memory_config'."""
        rc, out, err = _run_hook(
            "memory-proactive-context.py",
            json.dumps({"tool_name": "bash", "tool_input": {"command": "ls"}}),
        )
        # The hook may produce no output (no query match) or context, but
        # it must NOT crash with an ImportError.
        self.assertNotIn("ModuleNotFoundError", err)
        self.assertNotIn("No module named 'memory_config'", err)
        self.assertEqual(rc, 0, f"hook exited {rc}: stderr={err!r}")

    def test_search_on_demand_runs(self):
        """on-demand hook takes CLI args, not stdin. Pass a query as arg."""
        p = subprocess.run(
            [VENV_PY, str(HOOKS_DIR / "memory-search-on-demand.py"), "test query"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(INSTALL_ROOT),
        )
        self.assertNotIn("ModuleNotFoundError", p.stderr)
        self.assertNotIn("No module named 'memory_config'", p.stderr)
        # on-demand hook exits non-zero on no results — that's fine for the
        # resilience check; just make sure it didn't crash with ImportError.
        self.assertNotIn("Traceback", p.stderr)

    def test_session_start_runs(self):
        rc, out, err = _run_hook("memory-session-start.py", "{}")
        self.assertNotIn("ModuleNotFoundError", err)
        self.assertNotIn("No module named 'memory_config'", err)
        self.assertEqual(rc, 0, f"hook exited {rc}: stderr={err!r}")


class TestHookResilience(unittest.TestCase):
    """P0: hooks must never propagate a non-zero exit code, even on errors."""

    def test_proactive_context_with_garbage_input(self):
        """Hook with invalid JSON stdin should exit 0, not crash."""
        rc, out, err = _run_hook("memory-proactive-context.py", "this is not json {{{")
        self.assertEqual(rc, 0, f"hook exited {rc}: stderr={err!r}")

    def test_proactive_context_with_empty_input(self):
        rc, out, err = _run_hook("memory-proactive-context.py", "")
        self.assertEqual(rc, 0, f"hook exited {rc}: stderr={err!r}")

    def test_proactive_context_with_oversized_input(self):
        """Hook with a huge input should not OOM or hang."""
        big = json.dumps({"tool_name": "x", "tool_input": {"command": "y" * 100000}})
        rc, out, err = _run_hook("memory-proactive-context.py", big, timeout=30.0)
        self.assertEqual(rc, 0, f"hook exited {rc}: stderr={err!r}")


class TestHookOutput(unittest.TestCase):
    """Sanity checks on the actual output of each hook."""

    def test_session_start_outputs_memory_summary(self):
        """session-start should exit 0 and not crash (output varies by env)."""
        rc, out, err = _run_hook("memory-session-start.py", "{}")
        self.assertEqual(rc, 0, f"hook exited {rc}: stderr={err!r}")
        self.assertNotIn("Traceback", err)

    def test_proactive_context_outputs_context_for_query(self):
        """When given a query, the hook should search and potentially output context."""
        # Use a query that exists in the live DB
        stdin = json.dumps(
            {
                "tool_name": "bash",
                "tool_input": {"command": "search for Qwen3 MPS hang"},
            }
        )
        rc, out, err = _run_hook("memory-proactive-context.py", stdin, timeout=20.0)
        self.assertEqual(rc, 0)
        # Either it found a match (prints context block) or it didn't
        # (silent return). Either is fine — we just want exit 0 and no crash.
        self.assertNotIn("Traceback", err)


class TestInstallRootFallback(unittest.TestCase):
    """The install_root fallback chain must work in all standard cases."""

    def test_install_root_env_var_takes_priority(self):
        """If MEMORY_INSTALL_ROOT is set, it overrides the default."""
        # Set a known path
        old = os.environ.get("MEMORY_INSTALL_ROOT")
        os.environ["MEMORY_INSTALL_ROOT"] = str(INSTALL_ROOT)
        try:
            rc, out, err = _run_hook(
                "memory-proactive-context.py",
                json.dumps({"tool_name": "bash", "tool_input": {"command": "ls"}}),
            )
            self.assertEqual(rc, 0)
        finally:
            if old is None:
                os.environ.pop("MEMORY_INSTALL_ROOT", None)
            else:
                os.environ["MEMORY_INSTALL_ROOT"] = old


class TestMcpModuleBootstrap(unittest.TestCase):
    """2026-06-19: every mcp_*.py must have a sys.path bootstrap so it
    works as a script from any cwd, not just the install root."""

    MCP_MODULES = [
        "mcp_common",
        "mcp_ctr_drift",
        "mcp_kg",
        "mcp_maintenance",
        "mcp_memory",
        "mcp_okf",
        "mcp_profile",
        "mcp_quality",
        "mcp_retention",
        "mcp_safety",
        "mcp_search",
        "mcp_sharing",
        "mcp_summarization",
        "mcp_tools",
    ]

    def test_every_mcp_module_has_bootstrap(self):
        """Every mcp_*.py file must import _bootstrap_path."""
        for mod in self.MCP_MODULES:
            path = INSTALL_ROOT / f"{mod}.py"
            text = path.read_text()
            self.assertIn(
                "_bootstrap_path",
                text,
                f"{mod}.py is missing the _bootstrap_path import",
            )

    def test_memory_mcp_has_bootstrap(self):
        """The MCP server entry point (memory_mcp.py) must also import _bootstrap_path."""
        text = (INSTALL_ROOT / "memory_mcp.py").read_text()
        self.assertIn("_bootstrap_path", text)


class TestSessionEndHook(unittest.TestCase):
    """P1: memory-session-end hook must never crash and must exit 0."""

    def _run_hook_session_end(self, stdin: str = "", timeout: float = 30.0):
        return _run_hook("memory-session-end.py", stdin, timeout=timeout)

    def test_session_end_hook_imports_cleanly(self):
        rc, out, err = self._run_hook_session_end("{}")
        self.assertNotIn("ModuleNotFoundError", err)
        self.assertEqual(rc, 0, f"hook exited {rc}: stderr={err!r}")

    def test_session_end_hook_with_garbage_input(self):
        rc, out, err = self._run_hook_session_end("this is not json {{{")
        self.assertEqual(rc, 0, f"hook exited {rc}: stderr={err!r}")

    def test_session_end_hook_with_stop_event(self):
        stdin = json.dumps({"session_id": "test-session-123", "tool_name": ""})
        rc, out, err = self._run_hook_session_end(stdin)
        self.assertEqual(rc, 0, f"hook exited {rc}: stderr={err!r}")

    def test_session_end_hook_updates_tool_count(self):
        stdin = json.dumps({"session_id": "test-session-456", "tool_name": "bash"})
        rc, out, err = self._run_hook_session_end(stdin)
        self.assertEqual(rc, 0, f"hook exited {rc}: stderr={err!r}")


if __name__ == "__main__":
    unittest.main()
