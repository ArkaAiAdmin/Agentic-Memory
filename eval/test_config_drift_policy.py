"""Tests for infra/config_drift_policy.py — policy resolution and enforcement.

Subprocess-isolated per Hard Rule 20.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

WORKTREE = Path(__file__).resolve().parent.parent
VENV_PYTHON = WORKTREE / ".venv" / "bin" / "python"
if not VENV_PYTHON.exists():
    VENV_PYTHON = WORKTREE / "venv" / "bin" / "python"

# Empty TOML so subprocess tests don't inherit the worktree's memory.toml
# (which has [drift] tier_modes that would override default policies).
_EMPTY_TOML = Path(tempfile.mkdtemp()) / "empty.toml"
_EMPTY_TOML.write_text("")


def _run(code: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    import tempfile
    import shutil
    tmp_root = tempfile.mkdtemp()
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORY_")}
    env["PYTHONPATH"] = str(WORKTREE)
    env["MEMORY_INSTALL_ROOT"] = tmp_root
    env["MEMORY_CONFIG_PATH"] = str(_EMPTY_TOML)
    if extra_env:
        env.update(extra_env)
    try:
        return subprocess.run(
            [str(VENV_PYTHON), "-c", code],
            capture_output=True, text=True, timeout=30, env=env,
            cwd=str(WORKTREE),
        )
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


class TestPolicyDefaults(unittest.TestCase):
    """Test default policy resolution per scope."""

    def test_production_default(self):
        code = """
from infra.config_drift_policy import resolve_policy, DriftEnforceMode
policy = resolve_policy(scope="production")
assert policy.scope == "production"
assert policy.tier_modes["integrity"] == DriftEnforceMode.HARD_FAIL
assert policy.tier_modes["stability"] == DriftEnforceMode.HARD_FAIL
assert policy.tier_modes["compliance"] == DriftEnforceMode.SOFT_BLOCK
assert policy.tier_modes["operational"] == DriftEnforceMode.SOFT_BLOCK
assert policy.tier_modes["neutral"] == DriftEnforceMode.WARN
print(f"OK: production policy, hash={policy.policy_hash()}")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_test_default_policy(self):
        code = """
from infra.config_drift_policy import resolve_policy, DriftEnforceMode
policy = resolve_policy(scope="test")
assert policy.scope == "test"
for t in ("integrity", "stability", "compliance", "operational", "neutral"):
    assert policy.tier_modes[t] == DriftEnforceMode.WARN, f"tier {t} not WARN"
assert policy.soft_block_operations == []
print(f"OK: test policy, hash={policy.policy_hash()}")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_staging_default_policy(self):
        code = """
from infra.config_drift_policy import resolve_policy, DriftEnforceMode
policy = resolve_policy(scope="staging")
assert policy.scope == "staging"
assert policy.tier_modes["integrity"] == DriftEnforceMode.HARD_FAIL
assert policy.tier_modes["stability"] == DriftEnforceMode.SOFT_BLOCK
assert policy.tier_modes["compliance"] == DriftEnforceMode.WARN
assert policy.tier_modes["operational"] == DriftEnforceMode.WARN
assert policy.tier_modes["neutral"] == DriftEnforceMode.WARN
assert "save" in policy.soft_block_operations
print(f"OK: staging policy, hash={policy.policy_hash()}")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)


class TestPolicyHash(unittest.TestCase):
    """Test that policy_hash is stable and differentiates policies."""

    def test_stable_hash(self):
        code = """
from infra.config_drift_policy import resolve_policy, reset_policy_cache
h1 = resolve_policy(scope="production").policy_hash()
reset_policy_cache()
h2 = resolve_policy(scope="production").policy_hash()
assert h1 == h2, f"Hash changed: {h1} vs {h2}"
print(f"OK: stable hash={h1}")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_hash_differs_across_scopes(self):
        code = """
from infra.config_drift_policy import resolve_policy, reset_policy_cache
h_prod = resolve_policy(scope="production").policy_hash()
reset_policy_cache()
h_test = resolve_policy(scope="test").policy_hash()
assert h_prod != h_test, f"Hashes should differ: {h_prod} vs {h_test}"
print(f"OK: prod={h_prod} != test={h_test}")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_reset_and_reresolve_returns_same_hash(self):
        code = """
from infra.config_drift_policy import resolve_policy, reset_policy_cache
h1 = resolve_policy(scope="staging").policy_hash()
reset_policy_cache()
h2 = resolve_policy(scope="staging").policy_hash()
assert h1 == h2, f"Hash changed after reset: {h1} vs {h2}"
print(f"OK: hash stable after reset={h1}")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_reset_and_scope_change_produces_new_policy(self):
        code = """
from infra.config_drift_policy import resolve_policy, reset_policy_cache
h1 = resolve_policy(scope="staging").policy_hash()
reset_policy_cache()
h2 = resolve_policy(scope="development").policy_hash()
assert h1 != h2, f"Hashes should differ after scope change: {h1} vs {h2}"
print(f"OK: staging={h1} != development={h2}")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)


class TestPolicyTomlOverlay(unittest.TestCase):
    """Test that TOML [drift] section overlays work."""

    def test_toml_default_mode_overlay(self):
        code = """
import os, sys, tempfile
from pathlib import Path

toml_content = '[drift]\\ndefault_mode = "warn"\\n'
with tempfile.NamedTemporaryFile(mode='w', suffix='.toml', delete=False, prefix='test_drift_') as f:
    f.write(toml_content)
    toml_path = f.name

os.environ["MEMORY_CONFIG_PATH"] = toml_path
for mod in list(sys.modules.keys()):
    if 'config_drift' in mod or 'infra.config' == mod:
        del sys.modules[mod]

from infra.config_drift_policy import resolve_policy, DriftEnforceMode
# In a subprocess the _TOML_PATH is already resolved; we force scope to avoid auto-resolve
policy = resolve_policy(scope="production")
assert policy.default_mode == DriftEnforceMode.WARN, f"Expected WARN, got {policy.default_mode}"
# With default_mode=warn, all tiers that don't have an explicit override should be WARN
assert policy.tier_modes["integrity"] == DriftEnforceMode.HARD_FAIL, "explicit tier_modes should still apply"
print(f"OK: TOML overlay applied, default_mode={policy.default_mode.value}")
os.unlink(toml_path)
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)


if __name__ == "__main__":
    unittest.main()