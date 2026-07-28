"""Tests for config_drift snapshot persistence — round-trip and corruption.

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
    import tempfile
    import shutil
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


class TestPersistence(unittest.TestCase):
    """Test round-trip: build -> persist -> load."""

    def test_round_trip(self):
        code = """
import json, os, tempfile
from pathlib import Path
from infra.config_drift import (
    build_drift_report, persist_drift_report, load_last_drift_snapshot,
    _DRIFT_SNAPSHOT_PATH,
)

report = build_drift_report()

# Override snapshot path to a temp location
import infra.config_drift as cd
tmp = Path(tempfile.mkdtemp()) / "test_snapshot.json"
cd._DRIFT_SNAPSHOT_PATH = tmp

persisted = persist_drift_report(report)
assert persisted == tmp, f"persisted={persisted}, expected={tmp}"

loaded = load_last_drift_snapshot()
assert loaded is not None, "Expected non-None loaded report"
assert loaded.schema_version == 1, f"schema_version={loaded.schema_version}"
assert loaded.total_flags == report.total_flags, f"flags mismatch: {loaded.total_flags} != {report.total_flags}"
assert loaded.host == report.host, f"host mismatch"
print(f"OK: round-trip verified ({loaded.total_flags} flags)")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_corrupted_snapshot_returns_none(self):
        code = """
import json, os, tempfile
from pathlib import Path
from infra.config_drift import (
    build_drift_report, load_last_drift_snapshot,
    _DRIFT_SNAPSHOT_PATH,
)
import infra.config_drift as cd

tmp = Path(tempfile.mkdtemp()) / "test_snapshot.json"
cd._DRIFT_SNAPSHOT_PATH = tmp

# Write corrupt data
tmp.write_text("this is not valid json")

loaded = load_last_drift_snapshot()
assert loaded is None, f"Expected None for corrupt snapshot, got {loaded}"
print("OK: corrupt snapshot returns None")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_wrong_schema_version_returns_none(self):
        code = """
import json, os, tempfile
from pathlib import Path
from infra.config_drift import (
    build_drift_report, load_last_drift_snapshot, persist_drift_report,
    _DRIFT_SNAPSHOT_PATH,
)
import infra.config_drift as cd

tmp = Path(tempfile.mkdtemp()) / "test_snapshot.json"
cd._DRIFT_SNAPSHOT_PATH = tmp

# Write a v2 report
report = build_drift_report()
data = report.to_dict()
data["schema_version"] = 2
tmp.write_text(json.dumps(data))

loaded = load_last_drift_snapshot()
assert loaded is None, f"Expected None for schema_version=2, got {loaded}"
print("OK: schema_version=2 returns None")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)


if __name__ == "__main__":
    unittest.main()
