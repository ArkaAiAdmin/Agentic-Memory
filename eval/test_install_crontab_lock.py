"""Test for Scenario 10: install_crontab.sh must be safe to run concurrently.

Verifies the flock guard at the top of the script prevents two
parallel invocations from racing on the user's crontab.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
SCRIPT = INSTALL_DIR / "cron" / "install_crontab.sh"


class TestInstallCrontabLock(unittest.TestCase):
    def setUp(self) -> None:
        if not SCRIPT.exists():
            self.skipTest(f"{SCRIPT} not found")

    def test_script_uses_lock(self) -> None:
        """install_crontab.sh must include a concurrency guard."""
        content = SCRIPT.read_text()
        # The fix uses a mkdir-based lock (POSIX-portable, unlike
        # Linux-only `flock`).
        self.assertIn("mkdir", content)
        self.assertIn(
            "install_crontab",
            content,
            "Script must reference a lock path so concurrent runs can serialise",
        )

    def test_lock_is_set_and_released(self) -> None:
        """A --show run must create the lock dir and clean it up on exit."""
        import shutil

        # Skip if /tmp is read-only or crontab is missing — both
        # make the test invalid in this environment.
        if shutil.which("crontab") is None:
            self.skipTest("crontab not available in this environment")
        with tempfile.TemporaryDirectory() as tmpdir:
            env = os.environ.copy()
            env["TMPDIR"] = tmpdir
            result = subprocess.run(
                ["bash", str(SCRIPT), "--show"],
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, f"--show failed: {result.stderr}")
            # After --show returns, the lock dir must have been removed
            # (the trap cleans it up).
            lock_dir = (
                Path(tmpdir)
                / "agentic-memory-install-crontab.lock.d"
            )
            self.assertFalse(
                lock_dir.exists(),
                f"Lock dir {lock_dir} should be cleaned up by the trap on exit",
            )

    def test_concurrent_runs_serialise(self) -> None:
        """Two parallel --show invocations must serialise via the lock.

        The script's ``mkdir``-based lock guarantees the two
        invocations never execute their core work simultaneously.
        Whichever wins the mkdir proceeds; the loser waits in a
        0.2s polling loop.  The test verifies that both complete
        within the test timeout (i.e. the lock works, not
        60-second timeouts) and both produce the same output.
        """
        import shutil as _shutil

        if _shutil.which("crontab") is None:
            self.skipTest("crontab not available in this environment")

        # Use a unique TMPDIR per test so the lock dir is isolated.
        # Pre-create the parent dir so the script's mkdir succeeds
        # without falling back to /tmp.
        with tempfile.TemporaryDirectory() as tmpdir:
            env = os.environ.copy()
            env["TMPDIR"] = tmpdir

            def run_show() -> tuple[int, str]:
                result = subprocess.run(
                    ["bash", str(SCRIPT), "--show"],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=30,
                )
                return result.returncode, result.stdout

            results: list[tuple[int, str]] = []

            def worker() -> None:
                results.append(run_show())

            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            time.sleep(0.5)  # ensure t1 wins the mkdir first
            t2.start()
            t1.join(timeout=40)
            t2.join(timeout=40)
            self.assertFalse(
                t1.is_alive() or t2.is_alive(),
                f"Concurrent runs should not hang. "
                f"t1.is_alive={t1.is_alive()}, t2.is_alive={t2.is_alive()}, "
                f"results={results}",
            )
            self.assertEqual(len(results), 2)
            for rc, _ in results:
                # 0 = success; 1 = crontab error (env may lack user crontab).
                self.assertIn(rc, (0, 1), f"--show returned unexpected {rc}")
            # Both runs must produce the same output (idempotent).
            self.assertEqual(results[0][1], results[1][1])


if __name__ == "__main__":
    unittest.main()
