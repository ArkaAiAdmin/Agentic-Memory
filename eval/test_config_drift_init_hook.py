"""Tests for infra/config_drift_policy.py — run_startup_enforcement().

Subprocess-isolated per Hard Rule 20. Each test spawns a clean Python
subprocess with a controlled environment so cross-test pollution is
impossible.
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


def _run_in_subprocess(env_overrides: dict) -> subprocess.CompletedProcess:
    """Run ``run_startup_enforcement()`` in a fresh subprocess.

    Starts with a minimal env — does NOT inherit the parent's ``MEMORY_*``
    variables so test-setup drift (from conftest fixtures) doesn't leak
    into the subprocess.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "OBJC_DISABLE_INITIALIZE_FORK_SAFETY": "YES",
        "PYTHONPATH": str(WORKTREE),
        "MEMORY_INSTALL_ROOT": str(WORKTREE),
    }
    env.update(env_overrides)
    proc = subprocess.run(
        [
            str(VENV_PYTHON), "-c",
            "from infra.config_drift_policy import run_startup_enforcement; "
            "run_startup_enforcement()",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        cwd=str(WORKTREE),
    )
    return proc


class TestInitHookDefaultScope(unittest.TestCase):
    """Default scope (test) ⇒ no enforcement."""

    def test_default_scope_skips_enforcement(self) -> None:
        proc = _run_in_subprocess({"MEMORY_SCOPE": "test"})
        self.assertEqual(proc.returncode, 0)


class TestInitHookEscapeHatch(unittest.TestCase):
    """Escape hatch absorbs hard-fail drift."""

    def test_hatch_absorbs_hard_fail(self) -> None:
        proc = _run_in_subprocess({
            "MEMORY_SCOPE": "production",
            "MEMORY_SAGA_ENABLED": "0",
            "MEMORY_ESCAPE_HATCH": "ignore-integrity;testing;sre-bob;300;60",
        })
        self.assertEqual(proc.returncode, 0)


class TestInitHookNoHatch(unittest.TestCase):
    """No escape hatch → hard-fail exit 78."""

    def test_no_hatch_exits_78(self) -> None:
        proc = _run_in_subprocess({
            "MEMORY_SCOPE": "production",
            "MEMORY_SAGA_ENABLED": "0",
        })
        self.assertEqual(proc.returncode, 78)
        self.assertIn("FATAL: config drift on startup", proc.stderr)


class TestInitHookLegacyOptOut(unittest.TestCase):
    """MEMORY_FAIL_ON_INTEGRITY_DRIFT=0 legacy opt-out → code 0."""

    def test_legacy_optout(self) -> None:
        proc = _run_in_subprocess({
            "MEMORY_SCOPE": "production",
            "MEMORY_SAGA_ENABLED": "0",
            "MEMORY_FAIL_ON_INTEGRITY_DRIFT": "0",
        })
        self.assertEqual(proc.returncode, 0)


class TestInitHookNoDrift(unittest.TestCase):
    """No drift → exits 0 even under production scope."""

    def test_no_drift_exits_zero(self) -> None:
        proc = _run_in_subprocess({
            "MEMORY_SCOPE": "production",
        })
        self.assertEqual(proc.returncode, 0)


class TestInitHookStaging(unittest.TestCase):
    """Staging + integrity drift → hard_fail exit 78."""

    def test_staging_hard_fail(self) -> None:
        proc = _run_in_subprocess({
            "MEMORY_SCOPE": "staging",
            "MEMORY_SAGA_ENABLED": "0",
        })
        self.assertEqual(proc.returncode, 78)


if __name__ == "__main__":
    unittest.main()