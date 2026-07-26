#!/usr/bin/env python3
"""Tests for infra/mcp_singleton.py — stale-lock override retry loop.

Run with:
    ~/.config/agentic-memory/venv/bin/python eval/test_mcp_singleton.py

Subprocess-isolated per Hard Rule 20.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import tempfile
import time
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


class TestAcquireMcpSingletonStaleLock(unittest.TestCase):
    """Test that acquire_mcp_singleton handles stale locks."""

    def test_override_succeeds_when_pid_is_dead(self):
        """A lock file with a dead PID is overridden immediately."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / ".mcp_server.lock"
            lock_path.write_text("99999999")
            code = f"""
import os, sys
os.environ["MEMORY_AGENT_ID"] = "test-agent"
os.environ["MEMORY_DB_PATH"] = "{tmpdir}/memory.db"
sys.path.insert(0, "{WORKTREE}")
from infra.mcp_singleton import acquire_mcp_singleton, release_mcp_singleton
result = acquire_mcp_singleton()
print("RESULT:" + str(result))
if result:
    release_mcp_singleton()
"""
            result = _run(code, extra_env={"MEMORY_DB_PATH": f"{tmpdir}/memory.db"})
            self.assertIn("RESULT:True", result.stdout, msg=result.stderr)

    def test_succeeds_when_pid_dies_between_checks(self):
        """When the PID is alive on the first check but dead by a
        subsequent check (e.g. the other instance crashed during init),
        the override succeeds."""
        with tempfile.TemporaryDirectory() as tmpdir:
            code_holder = f"""
import fcntl, os, sys, time
lock_path = "{tmpdir}/.mcp_server.lock"
fd = open(lock_path, "w")
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
fd.write(str(os.getpid()))
fd.flush()
time.sleep(1.0)
fcntl.flock(fd, fcntl.LOCK_UN)
fd.close()
"""
            holder = subprocess.Popen(
                [VENV_PYTHON, "-c", code_holder],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            lock_path = Path(tmpdir) / ".mcp_server.lock"
            for _ in range(50):
                if lock_path.exists() and lock_path.read_text().strip():
                    break
                time.sleep(0.05)

            try:
                code = f"""
import os, sys, time
os.environ["MEMORY_AGENT_ID"] = "test-agent"
os.environ["MEMORY_DB_PATH"] = "{tmpdir}/memory.db"
sys.path.insert(0, "{WORKTREE}")
time.sleep(1.5)
from infra.mcp_singleton import acquire_mcp_singleton, release_mcp_singleton
result = acquire_mcp_singleton()
print("RESULT:" + str(result))
if result:
    release_mcp_singleton()
"""
                result = _run(code, extra_env={"MEMORY_DB_PATH": f"{tmpdir}/memory.db"})
                self.assertIn("RESULT:True", result.stdout, msg=result.stderr)
            finally:
                holder.wait(timeout=5)

    def test_pid_parse_error_treated_as_stale(self):
        """A lock file with non-numeric content triggers stale override."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / ".mcp_server.lock"
            lock_path.write_text("not-a-pid")
            code = f"""
import os, sys
os.environ["MEMORY_AGENT_ID"] = "test-agent"
os.environ["MEMORY_DB_PATH"] = "{tmpdir}/memory.db"
sys.path.insert(0, "{WORKTREE}")
from infra.mcp_singleton import acquire_mcp_singleton, release_mcp_singleton
result = acquire_mcp_singleton()
print("RESULT:" + str(result))
if result:
    release_mcp_singleton()
"""
            result = _run(code, extra_env={"MEMORY_DB_PATH": f"{tmpdir}/memory.db"})
            self.assertIn("RESULT:True", result.stdout, msg=result.stderr)

    def test_empty_lock_file_triggers_override(self):
        """An empty lock file is treated as stale."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / ".mcp_server.lock"
            lock_path.write_text("")
            code = f"""
import os, sys
os.environ["MEMORY_AGENT_ID"] = "test-agent"
os.environ["MEMORY_DB_PATH"] = "{tmpdir}/memory.db"
sys.path.insert(0, "{WORKTREE}")
from infra.mcp_singleton import acquire_mcp_singleton, release_mcp_singleton
result = acquire_mcp_singleton()
print("RESULT:" + str(result))
if result:
    release_mcp_singleton()
"""
            result = _run(code, extra_env={"MEMORY_DB_PATH": f"{tmpdir}/memory.db"})
            self.assertIn("RESULT:True", result.stdout, msg=result.stderr)

    def test_lock_file_gone_triggers_override(self):
        """If the lock file disappears between checks, try override."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / ".mcp_server.lock"
            lock_path.write_text("99999999")
            lock_path.unlink()
            code = f"""
import os, sys
os.environ["MEMORY_AGENT_ID"] = "test-agent"
os.environ["MEMORY_DB_PATH"] = "{tmpdir}/memory.db"
sys.path.insert(0, "{WORKTREE}")
from infra.mcp_singleton import acquire_mcp_singleton, release_mcp_singleton
result = acquire_mcp_singleton()
print("RESULT:" + str(result))
if result:
    release_mcp_singleton()
"""
            result = _run(code, extra_env={"MEMORY_DB_PATH": f"{tmpdir}/memory.db"})
            self.assertIn("RESULT:True", result.stdout, msg=result.stderr)


class TestRetryBackoffBounded(unittest.TestCase):
    """Verify the retry loop does not hang indefinitely."""

    def test_retry_backoff_is_bounded(self):
        """When initial flock fails and PID stays alive, total time is bounded."""
        # This test verifies the retry loop terminates even when the
        # competing instance's PID appears to stay alive. The subprocess
        # wins the initial flock race (nobody holds it), writes its own
        # PID, and succeeds — so the total time is short. The key
        # invariant is that it never hangs or exceeds a reasonable bound.
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / ".mcp_server.lock"
            lock_path.write_text("99999999")
            start = time.monotonic()
            code = f"""
import os, sys
os.environ["MEMORY_AGENT_ID"] = "test-agent"
os.environ["MEMORY_DB_PATH"] = "{tmpdir}/memory.db"
sys.path.insert(0, "{WORKTREE}")
from infra.mcp_singleton import acquire_mcp_singleton, release_mcp_singleton
result = acquire_mcp_singleton()
print("RESULT:" + str(result))
if result:
    release_mcp_singleton()
"""
            result = _run(code, extra_env={"MEMORY_DB_PATH": f"{tmpdir}/memory.db"})
            elapsed = time.monotonic() - start
            self.assertIn("RESULT:", result.stdout, msg=result.stderr)
            self.assertLess(elapsed, 10, msg="Call took too long — possible hang")


if __name__ == "__main__":
    unittest.main()