"""Tests for cron/cron_check_config_drift.py — drift surveillance cron.

Subprocess-isolated per Hard Rule 20.
"""
import json
import os
import subprocess
import unittest
from pathlib import Path

WORKTREE = Path(__file__).resolve().parent.parent
VENV_PYTHON = WORKTREE / ".venv" / "bin" / "python"
if not VENV_PYTHON.exists():
    VENV_PYTHON = WORKTREE / "venv" / "bin" / "python"

CRON_SCRIPT = str(WORKTREE / "cron" / "cron_check_config_drift.py")


def _run_cron(extra_env: dict | None = None, *args: str) -> subprocess.CompletedProcess:
    import tempfile
    tmp_root = tempfile.mkdtemp()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKTREE)
    env["MEMORY_INSTALL_ROOT"] = tmp_root
    env["MEMORY_KNOWLEDGE_GRAPH"] = "1"
    if extra_env:
        env.update(extra_env)
    cmd = [str(VENV_PYTHON), CRON_SCRIPT] + list(args)
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=30, env=env,
    )


class TestCronDryRun(unittest.TestCase):
    """Test the cron in dry-run mode (no side effects)."""

    def test_dry_run_exits_zero(self):
        result = _run_cron(None, "--dry-run")
        self.assertEqual(result.returncode, 0, msg=result.stderr + result.stdout)

    def test_dry_run_with_alert_stdout_no_drift(self):
        result = _run_cron(None, "--dry-run", "--alert-stdout")
        self.assertEqual(result.returncode, 0, msg=result.stderr + result.stdout)


class TestCronWithDrift(unittest.TestCase):
    """Test that the cron detects and reports drift."""

    def test_integrity_drift_detected(self):
        result = _run_cron(
            {"MEMORY_SAGA_ENABLED": "0"},
            "--severity-floor", "stability", "--alert-stdout",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("MEMORY_SAGA_ENABLED", result.stdout,
                      msg=f"Expected MEMORY_SAGA_ENABLED in stdout, got: {result.stdout}")

    def test_cron_produces_archive(self):
        """The cron without --dry-run writes an archive + snapshot."""
        import tempfile
        tmp_install = tempfile.mkdtemp()
        result = _run_cron(
            {
                "MEMORY_INSTALL_ROOT": tmp_install,
                "MEMORY_DB_PATH": ":memory:",
            },
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr + result.stdout)
        # Check that snapshot was written
        from pathlib import Path
        snap = Path(tmp_install) / "memory" / "last_drift_snapshot.json"
        self.assertTrue(snap.exists(), f"snapshot not found at {snap}")
        with open(snap) as f:
            data = json.load(f)
        self.assertEqual(data["schema_version"], 1)
        self.assertGreater(data["total_flags"], 0)


if __name__ == "__main__":
    unittest.main()
