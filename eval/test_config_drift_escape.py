"""Tests for infra/config_drift_escape.py — time-bounded escape hatch.

Subprocess-isolated per Hard Rule 20. Each test runs as a subprocess
so the conftest autouse fixture doesn't trigger the config deadlock.
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


def _run(code: str) -> subprocess.CompletedProcess:
    import tempfile, shutil
    tmp_root = tempfile.mkdtemp()
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORY_")}
    env["PYTHONPATH"] = str(WORKTREE)
    env["MEMORY_INSTALL_ROOT"] = tmp_root
    try:
        return subprocess.run(
            [str(VENV_PYTHON), "-c", code],
            capture_output=True, text=True, timeout=30, env=env,
            cwd=str(WORKTREE),
        )
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


SETUP = """
import os, sys
sys.path.insert(0, {WORKTREE!r})
from unittest.mock import patch
from infra.config_drift_escape import register_escape_hatch, reset_escape_hatch

class _MockPolicy:
    escape_hatch_enabled = True
    escape_hatch_max_secs = 14400
    escape_hatch_audit_every_secs = 60
    scope = "test"
    audit_path = "/tmp/test_drift_audit.jsonl"
    def policy_hash(self):
        return "testhash"

reset_escape_hatch()
""".format(WORKTREE=str(WORKTREE))


class TestConfigDriftEscape(unittest.TestCase):
    """Exercises the escape hatch lifecycle via subprocess isolation."""

    # ------------------------------------------------------------------
    # Test 1: Valid hatch parses and is active
    # ------------------------------------------------------------------

    def test_valid_hatch(self) -> None:
        code = SETUP + """
with patch("infra.config_drift_escape.time.time", return_value=1000.0):
    policy = _MockPolicy()
    hatch = register_escape_hatch(policy, env_value="ignore-integrity;reason;op;300;60")
assert hatch is not None, "Expected non-None hatch"
assert hatch.scope == "ignore-integrity", f"scope={hatch.scope!r}"
assert hatch.reason == "reason", f"reason={hatch.reason!r}"
assert hatch.operator_id == "op", f"operator_id={hatch.operator_id!r}"
assert hatch.duration_secs == 300, f"duration={hatch.duration_secs}"
assert hatch.issued_at == 1000.0, f"issued_at={hatch.issued_at}"
assert hatch.is_active(now=1000.0) is True
print("OK: valid_hatch")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    # ------------------------------------------------------------------
    # Test 2: Empty reason -> rejected, None
    # ------------------------------------------------------------------

    def test_empty_reason(self) -> None:
        code = SETUP + """
with patch("infra.config_drift_escape.time.time", return_value=1000.0):
    policy = _MockPolicy()
    hatch = register_escape_hatch(policy, env_value="ignore-integrity;;op;300;60")
assert hatch is None, f"Expected None, got {hatch}"
print("OK: empty_reason")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    # ------------------------------------------------------------------
    # Test 3: Empty operator -> rejected, None
    # ------------------------------------------------------------------

    def test_empty_operator(self) -> None:
        code = SETUP + """
with patch("infra.config_drift_escape.time.time", return_value=1000.0):
    policy = _MockPolicy()
    hatch = register_escape_hatch(policy, env_value="ignore-integrity;reason;;300;60")
assert hatch is None, f"Expected None, got {hatch}"
print("OK: empty_operator")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    # ------------------------------------------------------------------
    # Test 4: Duration > max -> clamped to max
    # ------------------------------------------------------------------

    def test_duration_clamping(self) -> None:
        code = SETUP + """
with patch("infra.config_drift_escape.time.time", return_value=1000.0):
    policy = _MockPolicy()
    hatch = register_escape_hatch(policy, env_value="ignore-integrity;reason;op;99999;60")
assert hatch is not None, "Expected non-None hatch"
assert hatch.duration_secs == policy.escape_hatch_max_secs, \
    f"duration={hatch.duration_secs} != max={policy.escape_hatch_max_secs}"
assert hatch.max_duration_secs == policy.escape_hatch_max_secs
print("OK: duration_clamping")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    # ------------------------------------------------------------------
    # Test 5: Reaffirm < 30s -> clamped to 30
    # ------------------------------------------------------------------

    def test_reaffirm_clamping(self) -> None:
        code = SETUP + """
with patch("infra.config_drift_escape.time.time", return_value=1000.0):
    policy = _MockPolicy()
    hatch = register_escape_hatch(policy, env_value="ignore-integrity;reason;op;300;5")
assert hatch is not None, "Expected non-None hatch"
assert hatch.affirmation_interval_secs == 30, \
    f"affirmation_interval={hatch.affirmation_interval_secs}"
print("OK: reaffirm_clamping")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    # ------------------------------------------------------------------
    # Test 6: Hatch expiry - is_active() becomes False after duration
    # ------------------------------------------------------------------

    def test_hatch_expiry(self) -> None:
        code = SETUP + """
with patch("infra.config_drift_escape.time.time", return_value=1000.0):
    policy = _MockPolicy()
    hatch = register_escape_hatch(policy, env_value="ignore-integrity;reason;op;300;60")
assert hatch is not None, "Expected non-None hatch"
assert hatch.is_active(now=1000.0) is True, "should be active at t=1000"
assert hatch.is_active(now=1301.0) is False, "should NOT be active at t=1301"
print("OK: hatch_expiry")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    # ------------------------------------------------------------------
    # Test 7: Unknown scope -> rejected, None
    # ------------------------------------------------------------------

    def test_unknown_scope(self) -> None:
        code = SETUP + """
with patch("infra.config_drift_escape.time.time", return_value=1000.0):
    policy = _MockPolicy()
    hatch = register_escape_hatch(policy, env_value="ignore-unknown;reason;op;300;60")
assert hatch is None, f"Expected None, got {hatch}"
print("OK: unknown_scope")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
