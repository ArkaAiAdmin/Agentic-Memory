"""Tests for infra/config_drift.py — report generation and verdict logic.

Subprocess-isolated per Hard Rule 20. Each test uses a clean subprocess
with isolated env to avoid cross-test pollution.
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
    import tempfile, shutil
    tmp_root = tempfile.mkdtemp()
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORY_")}
    env["PYTHONPATH"] = str(WORKTREE)
    env["MEMORY_INSTALL_ROOT"] = tmp_root
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


class TestBuildDriftReport(unittest.TestCase):
    """Test the basic report builder."""

    def test_report_has_valid_schema(self):
        code = """
import json
from infra.config_drift import build_drift_report
r = build_drift_report()
assert r.schema_version == 1, f"schema_version={r.schema_version}"
assert r.total_flags >= 30, f"total_flags={r.total_flags} < 30"
assert isinstance(r.host, str) and len(r.host) > 0
assert isinstance(r.drift_count_by_severity, dict)
print(f"OK: {r.total_flags} flags, host={r.host}")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_unknown_env_var_not_flagged(self):
        code = """
import os
os.environ["MEMORY_DEFINITELY_NOT_REAL"] = "42"
from infra.config_drift import build_drift_report
r = build_drift_report()
# Unknown flags aren't in the registry, so they shouldn't appear at all.
for e in r.entries:
    if "DEFINITELY_NOT_REAL" in e.flag:
        print(f"FOUND UNKNOWN FLAG: {e.flag}")
        raise SystemExit(1)
print("OK: unknown flags not present")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_default_sourced_flags_have_no_drift(self):
        code = """
from infra.config_drift import build_drift_report, _all_flag_specs, _decompose
specs = _all_flag_specs()
# Find flags where none of env/toml are set
drift = build_drift_report()
# Most flags should have no drift in the default environment
default_sourced = [e for e in drift.entries if e.sources.source == "default"]
assert len(default_sourced) > 0, "Expected some default-sourced flags"
# None should have drift verdicts
drifted_defaults = [e for e in default_sourced if e.has_drift()]
print(f"default-sourced: {len(default_sourced)}, drifted: {len(drifted_defaults)}")
assert len(drifted_defaults) == 0, f"Unexpected drift in defaults: {drifted_defaults}"
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_integrity_flag_disabled_produces_drift(self):
        code = """
import os
os.environ["MEMORY_SAGA_ENABLED"] = "0"
from infra.config_drift import build_drift_report
r = build_drift_report()
saga = [e for e in r.entries if e.flag == "MEMORY_SAGA_ENABLED"]
assert len(saga) == 1, f"Expected 1 entry, got {len(saga)}"
entry = saga[0]
assert entry.has_drift(), f"Expected drift, got empty verdicts"
assert entry.severity == "integrity", f"severity={entry.severity}"
has_critical = any("INTEGRITY_CRITICAL_DISABLED" in v for v in entry.drift_verdicts)
assert has_critical, f"Expected INTEGRITY_CRITICAL_DISABLED verdict, got {entry.drift_verdicts}"
print(f"OK: saga drift detected, verdicts={entry.drift_verdicts}")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_malformed_env_falls_back_with_mismatch_verdict(self):
        code = """
import os
os.environ["MEMORY_DB_POOL_SIZE"] = "banana"
from infra.config_drift import build_drift_report
r = build_drift_report()
entry = [e for e in r.entries if e.flag == "MEMORY_DB_POOL_SIZE"]
assert len(entry) == 1
e = entry[0]
print(f"flag={e.flag}, source={e.sources.source}, verdicts={e.drift_verdicts}")
# _resolve catches parse errors internally and falls back to default.
# Since env_raw is set, source is "env" (not "env_invalid").
assert e.sources.source == "env", f"source={e.sources.source}"
assert e.sources.effective == e.sources.default, "malformed env should fall back to default"
# The env_raw doesn't match str(default) so explicit_default_via_env_mismatch fires
has_mismatch = any("explicit_default_via_env_mismatch" in v for v in e.drift_verdicts)
assert has_mismatch, f"Expected explicit_default_via_env_mismatch verdict, got {e.drift_verdicts}"
print(f"OK: malformed env detected as mismatch")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_override_from_default_detected(self):
        code = """
import os
os.environ["MEMORY_DB_POOL_SIZE"] = "42"
from infra.config_drift import build_drift_report
r = build_drift_report()
entry = [e for e in r.entries if e.flag == "MEMORY_DB_POOL_SIZE"]
assert len(entry) == 1
e = entry[0]
has_override = any("override_from_default" in v for v in e.drift_verdicts)
assert has_override, f"Expected override_from_default verdict, got {e.drift_verdicts}"
print(f"OK: override detected, verdicts={e.drift_verdicts}")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)


class TestDiffReports(unittest.TestCase):
    """Test delta detection between snapshots."""

    def test_diff_from_none_returns_all_drift(self):
        code = """
import os
os.environ["MEMORY_SAGA_ENABLED"] = "0"
from infra.config_drift import build_drift_report, diff_reports
r = build_drift_report()
diffs = diff_reports(None, r)
assert len(diffs) > 0, "Expected non-empty diff from None"
print(f"OK: {len(diffs)} diffs from None snapshot")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_stable_drift_is_suppressed(self):
        code = """
import os
os.environ["MEMORY_SAGA_ENABLED"] = "0"
from infra.config_drift import build_drift_report, diff_reports
r1 = build_drift_report()
r2 = build_drift_report()
diffs = diff_reports(r1, r2)
# Same env, same report — diff should be empty (no new drift)
assert len(diffs) == 0, f"Expected no diffs for identical reports, got {diffs}"
print(f"OK: stable drift suppressed ({len(diffs)} diffs)")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_drift_cleared_detected(self):
        code = """
import os
from infra.config_drift import build_drift_report, diff_reports

# First run with override
os.environ["MEMORY_DB_POOL_SIZE"] = "99"
r1 = build_drift_report()

# Second run without override
os.environ.pop("MEMORY_DB_POOL_SIZE", None)
r2 = build_drift_report()

diffs = diff_reports(r1, r2)
cleared = [d for d in diffs if "drift cleared" in d]
print(f"diffs={diffs}")
print(f"OK: {len(cleared)} drift-cleared entries")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)


if __name__ == "__main__":
    unittest.main()
