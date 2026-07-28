"""Tests for infra/config_drift_audit.py — audit trail.

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


class TestDriftAudit(unittest.TestCase):
    """Test drift audit trail."""

    def test_append_and_read_round_trip(self):
        code = """
import os, tempfile
from pathlib import Path
from infra.config_drift_audit import append_audit_event, read_audit_events, AuditEvent

audit_path = Path(tempfile.mkdtemp()) / "test_audit.jsonl"
ev = AuditEvent(
    timestamp=1000.0, scope="test", decision="warn",
    tier="integrity", flag="MEMORY_SAGA_ENABLED", mode="warn",
    operator_id="op1", reason="override detected",
    progression_hits=1, policy_hash="abc123",
)
append_audit_event(ev, audit_path=str(audit_path))

events = read_audit_events(audit_path=str(audit_path))
assert len(events) == 1, f"Expected 1 event, got {len(events)}"
e = events[0]
assert e.timestamp == 1000.0
assert e.scope == "test"
assert e.decision == "warn"
assert e.tier == "integrity"
assert e.flag == "MEMORY_SAGA_ENABLED"
assert e.mode == "warn"
assert e.operator_id == "op1"
assert e.reason == "override detected"
assert e.progression_hits == 1
assert e.policy_hash == "abc123"
print("OK: round-trip fields intact")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_malformed_json_line_is_skipped(self):
        code = """
import os, tempfile
from pathlib import Path
from infra.config_drift_audit import append_audit_event, read_audit_events, AuditEvent

audit_path = Path(tempfile.mkdtemp()) / "test_audit.jsonl"

# Write a good event
ev = AuditEvent(
    timestamp=1.0, scope="test", decision="warn",
    tier="integrity", flag="F1", mode="strict",
)
append_audit_event(ev, audit_path=str(audit_path))

# Manually inject a bad line
with open(audit_path, "a") as f:
    f.write("not-valid-json\\n")

# Write another good event
ev2 = AuditEvent(
    timestamp=2.0, scope="test", decision="block",
    tier="stability", flag="F2", mode="strict",
)
append_audit_event(ev2, audit_path=str(audit_path))

events = read_audit_events(audit_path=str(audit_path))
assert len(events) == 2, f"Expected 2 valid events, got {len(events)}"
print(f"OK: {len(events)} events after skipping malformed line")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_decision_filter_returns_only_matching(self):
        code = """
import os, tempfile
from pathlib import Path
from infra.config_drift_audit import append_audit_event, read_audit_events, AuditEvent

audit_path = Path(tempfile.mkdtemp()) / "test_audit.jsonl"

append_audit_event(AuditEvent(1.0, "test", "warn", "integrity", "F1", "strict"), audit_path=str(audit_path))
append_audit_event(AuditEvent(2.0, "test", "soft_block", "stability", "F2", "strict"), audit_path=str(audit_path))
append_audit_event(AuditEvent(3.0, "test", "hard_fail", "integrity", "F3", "strict"), audit_path=str(audit_path))
append_audit_event(AuditEvent(4.0, "test", "hard_fail", "compliance", "F4", "strict"), audit_path=str(audit_path))

filtered = read_audit_events(audit_path=str(audit_path), decision_filter="hard_fail")
assert len(filtered) == 2, f"Expected 2 hard_fail events, got {len(filtered)}"
for e in filtered:
    assert e.decision == "hard_fail", f"Unexpected decision: {e.decision}"
print("OK: filtered 2 hard_fail events")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_since_ts_filters_returns_only_newer(self):
        code = """
import os, tempfile
from pathlib import Path
from infra.config_drift_audit import append_audit_event, read_audit_events, AuditEvent

audit_path = Path(tempfile.mkdtemp()) / "test_audit.jsonl"

append_audit_event(AuditEvent(10.0, "test", "warn", "integrity", "F1", "strict"), audit_path=str(audit_path))
append_audit_event(AuditEvent(20.0, "test", "warn", "integrity", "F2", "strict"), audit_path=str(audit_path))
append_audit_event(AuditEvent(30.0, "test", "warn", "integrity", "F3", "strict"), audit_path=str(audit_path))

filtered = read_audit_events(audit_path=str(audit_path), since_ts=25.0)
assert len(filtered) == 1, f"Expected 1 event after ts=25, got {len(filtered)}"
assert filtered[0].timestamp == 30.0
print("OK: since_ts filter works")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_rotation_renames_oversized_file(self):
        code = """
import os, tempfile
from pathlib import Path
from infra.config_drift_audit import append_audit_event, AuditEvent

audit_dir = Path(tempfile.mkdtemp())
audit_path = audit_dir / "test_audit.jsonl"

# Create a file bigger than max_bytes
oversized = b"A" * 60_000_000
audit_path.write_bytes(oversized)

ev = AuditEvent(
    timestamp=99.0, scope="test", decision="warn",
    tier="integrity", flag="F_ROTATE", mode="strict",
)
append_audit_event(ev, audit_path=str(audit_path))

backup = audit_path.with_suffix(audit_path.suffix + ".1")
assert backup.exists(), f"Expected backup at {backup}"
assert audit_path.exists(), "Original should be recreated"
content = audit_path.read_text()
assert "F_ROTATE" in content, f"Rotated file should contain new event"
print("OK: rotation created .1 backup and new file has event")
"""
        result = _run(code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)


if __name__ == "__main__":
    unittest.main()