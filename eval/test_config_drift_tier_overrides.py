"""Tests for TOML tier override loading in infra/config_drift.py.

Subprocess-isolated per Hard Rule 20.
"""
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


class TestTierOverrideViaAPI(unittest.TestCase):
    """Test set_flag_tier + _tier_for directly."""

    def test_set_tier_and_retrieve(self):
        code = """
from infra.config_drift import set_flag_tier, _tier_for, DriftSeverity
set_flag_tier("MEMORY_RERANKER_DISABLED", DriftSeverity.COMPLIANCE)
result = _tier_for("MEMORY_RERANKER_DISABLED")
assert result == DriftSeverity.COMPLIANCE, f"Expected COMPLIANCE, got {result}"
print(f"OK: MEMORY_RERANKER_DISABLED tier={result.value}")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_unknown_flag_defaults_to_neutral(self):
        code = """
from infra.config_drift import _tier_for, DriftSeverity
result = _tier_for("THIS_FLAG_DOES_NOT_EXIST")
assert result == DriftSeverity.NEUTRAL, f"Expected NEUTRAL, got {result}"
print(f"OK: unknown flag tier={result.value}")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_unlisted_flag_in_FLAG_TIERS_defaults_to_neutral(self):
        code = """
from infra.config_drift import _tier_for, DriftSeverity
result = _tier_for("MEMORY_SOME_UNKNOWN_FLAG")
assert result == DriftSeverity.NEUTRAL, f"Expected NEUTRAL, got {result}"
print(f"OK: unlisted flag tier={result.value}")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)


class TestApplyTierOverridesFromToml(unittest.TestCase):
    """Test that _apply_tier_overrides_from_toml works end-to-end."""

    def test_override_via_toml_file(self):
        code = """
import os, sys, tempfile
from pathlib import Path

# Write a temp TOML with a drift_tiers override
toml_content = '[drift_tiers]\\nMEMORY_RERANKER_DISABLED = "compliance"\\n'
with tempfile.NamedTemporaryFile(mode='w', suffix='.toml', delete=False, prefix='test_drift_') as f:
    f.write(toml_content)
    toml_path = f.name

os.environ["MEMORY_CONFIG_PATH"] = toml_path
# Clear any cached config parsing state
for mod in list(sys.modules.keys()):
    if 'config_drift' in mod or mod == 'infra.config':
        del sys.modules[mod]

from infra.config_drift import _tier_for, DriftSeverity
result = _tier_for("MEMORY_RERANKER_DISABLED")
assert result == DriftSeverity.COMPLIANCE, f"Expected COMPLIANCE, got {result}"
print(f"OK: MEMORY_RERANKER_DISABLED tier={result.value}")
os.unlink(toml_path)
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_malformed_tier_value_ignored(self):
        code = """
import os, sys, tempfile
from pathlib import Path

toml_content = '[drift_tiers]\\nMEMORY_RERANKER_DISABLED = "bogus_tier"\\n'
with tempfile.NamedTemporaryFile(mode='w', suffix='.toml', delete=False, prefix='test_drift_') as f:
    f.write(toml_content)
    toml_path = f.name

os.environ["MEMORY_CONFIG_PATH"] = toml_path
for mod in list(sys.modules.keys()):
    if 'config_drift' in mod or mod == 'infra.config':
        del sys.modules[mod]

from infra.config_drift import _tier_for, DriftSeverity
# 'bogus_tier' is not a valid DriftSeverity, override is ignored
result = _tier_for("MEMORY_RERANKER_DISABLED")
# Original tier is OPERATIONAL
assert result == DriftSeverity.OPERATIONAL, f"Expected OPERATIONAL, got {result}"
print(f"OK: malformed tier ignored, flag remains {result.value}")
os.unlink(toml_path)
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)


if __name__ == "__main__":
    unittest.main()