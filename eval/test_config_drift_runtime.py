"""Tests for infra/config_drift_runtime.py — drift progression tracker.

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
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(VENV_PYTHON), "-c", code],
        capture_output=True, text=True, timeout=30, env=env,
        cwd=str(WORKTREE),
    )


class TestDriftRuntime(unittest.TestCase):
    """Test drift progression tracking."""

    def test_three_hits_within_window_triggers_escalation(self):
        code = """
import time
from infra.config_drift_runtime import (
    record_drift, should_escalate, mark_escalated, reset_history,
)

class FakePolicy:
    progression_window_secs = 600
    progression_max_hits = 3

reset_history()
tier = "integrity"
record_drift(tier, window_secs=600)
record_drift(tier, window_secs=600)
record_drift(tier, window_secs=600)

assert should_escalate(tier, FakePolicy()), "Expected escalation after 3 hits"
print("OK: escalation triggered at 3 hits")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_old_hits_outside_window_do_not_trigger_escalation(self):
        code = """
import time
from infra.config_drift_runtime import (
    record_drift, should_escalate, mark_escalated, reset_history,
)

class FakePolicy:
    progression_window_secs = 0.001
    progression_max_hits = 3

reset_history()
tier = "stability"
record_drift(tier, window_secs=600)
time.sleep(0.01)
record_drift(tier, window_secs=600)

assert not should_escalate(tier, FakePolicy()), "Expected no escalation with old hits"
print("OK: old hits outside window do not escalate")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_mark_escalated_prevents_double_escalation(self):
        code = """
from infra.config_drift_runtime import (
    record_drift, should_escalate, mark_escalated, reset_history,
)

class FakePolicy:
    progression_window_secs = 600
    progression_max_hits = 3

reset_history()
tier = "compliance"
record_drift(tier, window_secs=600)
record_drift(tier, window_secs=600)
record_drift(tier, window_secs=600)

assert should_escalate(tier, FakePolicy()), "Expected escalation before mark"
mark_escalated(tier)
assert not should_escalate(tier, FakePolicy()), "Expected no escalation after mark"
print("OK: mark_escalated prevents double-escalation")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_reset_history_clears_only_target_tier(self):
        code = """
from infra.config_drift_runtime import (
    record_drift, get_hits, reset_history,
)

reset_history()
record_drift("integrity", window_secs=600)
record_drift("stability", window_secs=600)
record_drift("stability", window_secs=600)

assert get_hits("integrity") == 1
assert get_hits("stability") == 2

reset_history("integrity")
assert get_hits("integrity") == 0, "integrity should be cleared"
assert get_hits("stability") == 2, "stability should remain"
print("OK: reset_history clears only target tier")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)


if __name__ == "__main__":
    unittest.main()