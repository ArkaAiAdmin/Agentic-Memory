"""Tests for infra/scope.py — scope resolution logic.

Subprocess-isolated per Hard Rule 20.
"""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

WORKTREE = Path(__file__).resolve().parent.parent
VENV_PYTHON = WORKTREE / ".venv" / "bin" / "python"
if not VENV_PYTHON.exists():
    VENV_PYTHON = WORKTREE / "venv" / "bin" / "python"


def _run(code: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORY_")}
    env["PYTHONPATH"] = str(WORKTREE)
    env["MEMORY_INSTALL_ROOT"] = str(WORKTREE)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(VENV_PYTHON), "-c", code],
        capture_output=True, text=True, timeout=30, env=env,
        cwd=str(WORKTREE),
    )


class TestScope(unittest.TestCase):
    """Test scope resolution."""

    def test_memory_scope_env_wins(self):
        code = """
import os
os.environ["MEMORY_SCOPE"] = "test"
from infra.scope import resolve_scope, Scope
s = resolve_scope()
assert s == Scope.TEST, f"Expected TEST, got {s}"
print(f"OK: scope={s}")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_toml_scope_overrides_heuristic(self):
        code = """
import os, tempfile
from pathlib import Path

# Create a temp TOML with scope=production
tmp = tempfile.mkdtemp()
toml_path = Path(tmp) / "memory.toml"
toml_path.write_text("[scope]\\nname = \\"production\\"\\n")

import infra.config as cfg_mod
original_toml = getattr(cfg_mod, "_TOML_PATH")
setattr(cfg_mod, "_TOML_PATH", toml_path)

from infra.scope import resolve_scope, Scope
s = resolve_scope()
assert s == Scope.PRODUCTION, f"Expected PRODUCTION, got {s}"
print(f"OK: scope={s}")

setattr(cfg_mod, "_TOML_PATH", original_toml)
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_heuristic_returns_test_when_pytest_in_modules(self):
        code = """
import sys, types
sys.modules["pytest"] = types.ModuleType("pytest")
from infra.scope import resolve_scope, Scope
s = resolve_scope()
assert s == Scope.TEST, f"Expected TEST, got {s}"
print(f"OK: scope={s}")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_heuristic_returns_development_by_default(self):
        code = """
import os, sys
os.environ.pop("MEMORY_SCOPE", None)
os.environ.pop("MEMORY_INSTALL_ROOT", None)
if "pytest" in sys.modules:
    del sys.modules["pytest"]

import infra.config as cfg_mod
original_toml = getattr(cfg_mod, "_TOML_PATH")

from pathlib import Path
setattr(cfg_mod, "_TOML_PATH", Path("/nonexistent/path/to/memory.toml"))

from infra.scope import resolve_scope, Scope
s = resolve_scope()
assert s == Scope.DEVELOPMENT, f"Expected DEVELOPMENT, got {s}"
print(f"OK: scope={s}")

setattr(cfg_mod, "_TOML_PATH", original_toml)
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)


if __name__ == "__main__":
    unittest.main()