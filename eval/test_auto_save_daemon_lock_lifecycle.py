"""Regression tests for the auto-save daemon lock/pid lifecycle.

Covers the ownership-gating fixes for the spawn/idle-exit thrash loop:

  1. ``_cleanup_stale_daemon_lock`` must NEVER unlink the lock file when
     flock acquisition fails (a live holder exists) — deleting the path
     while another process flocks the inode makes that holder invisible
     to every liveness check.
  2. ``_remove_pid_file`` is ownership-gated: a foreign PID file (written
     by a successor daemon) must survive an unrelated caller's removal.
"""

import fcntl
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.chdir("..")
import sys

sys.path.insert(0, os.getcwd())


class TestDaemonLockLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="daemonlock_"))
        self.memory_dir = self.tmpdir / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self._orig_db = os.environ.get("MEMORY_DB_PATH")
        os.environ["MEMORY_DB_PATH"] = str(self.tmpdir / "memory.db")

    def tearDown(self):
        if self._orig_db:
            os.environ["MEMORY_DB_PATH"] = self._orig_db
        else:
            os.environ.pop("MEMORY_DB_PATH", None)

        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _lock_path(self) -> Path:
        from background.inbox import get_auto_save_lock_path

        return get_auto_save_lock_path()

    def _pid_path(self) -> Path:
        from background.inbox import get_auto_save_pid_path

        return get_auto_save_pid_path()

    def _hold_flock_in_subprocess(self, path: Path) -> subprocess.Popen:
        """Spawn a child that holds an exclusive flock on *path* until killed."""
        script = (
            "import fcntl, sys, time\n"
            "f = open(sys.argv[1], 'w')\n"
            "fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "print('HELD', flush=True)\n"
            "time.sleep(60)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        assert proc.stdout is not None
        line = proc.stdout.readline().strip()
        assert line == "HELD", f"child failed to hold flock: {line!r}"
        return proc

    def test_cleanup_never_unlinks_live_held_lock(self):
        """Stale PID + live flock holder => no deletion, returns False."""
        import time

        from background.inbox import (
            _cleanup_stale_daemon_lock,
            _is_daemon_lock_held,
        )

        lock_path = self._lock_path()
        pid_path = self._pid_path()
        # Dead PID in the file — the stale-PID precondition for cleanup.
        dead_pid = self._spawn_dead_pid()
        pid_path.write_text(str(dead_pid))

        holder = self._hold_flock_in_subprocess(lock_path)
        try:
            # Give the child a beat to actually hold the flock.
            deadline = time.time() + 5
            while time.time() < deadline and not _is_daemon_lock_held():
                time.sleep(0.05)
            self.assertTrue(_is_daemon_lock_held())

            cleaned = _cleanup_stale_daemon_lock()
            self.assertFalse(cleaned)
            # THE regression: the live holder's lock FILE must survive.
            self.assertTrue(
                lock_path.exists(),
                "_cleanup_stale_daemon_lock deleted a live-held lock file",
            )
            self.assertTrue(_is_daemon_lock_held())
        finally:
            holder.kill()
            holder.wait(timeout=10)

    def test_cleanup_removes_fully_stale_lock(self):
        """Dead PID + acquirable flock => cleanup removes the stale PID file."""
        from background.inbox import _cleanup_stale_daemon_lock

        lock_path = self._lock_path()
        lock_path.touch()
        dead_pid = self._spawn_dead_pid()
        self._pid_path().write_text(str(dead_pid))

        cleaned = _cleanup_stale_daemon_lock()
        self.assertTrue(cleaned)
        self.assertFalse(self._pid_path().exists())
        # The lock file itself is kept (only its stale claim was released).
        self.assertTrue(lock_path.exists())

    def test_remove_pid_file_is_ownership_gated(self):
        """A foreign PID file must NOT be removed by _remove_pid_file."""
        from background.inbox import _remove_pid_file

        pid_path = self._pid_path()
        foreign_pid = self._spawn_dead_pid()
        pid_path.write_text(str(foreign_pid))

        _remove_pid_file()
        self.assertTrue(
            pid_path.exists(),
            "_remove_pid_file deleted another daemon's PID file",
        )

        # Our own PID file IS removable.
        pid_path.write_text(str(os.getpid()))
        _remove_pid_file()
        self.assertFalse(pid_path.exists())

    @staticmethod
    def _spawn_dead_pid() -> int:
        """Fork a short-lived child and return its (now dead) PID."""
        script = "import sys; sys.exit(0)"
        subprocess.run([sys.executable, "-c", script], check=True)
        # A PID we know is dead: fork one via os.fork-less approach —
        # use the completed subprocess's real PID by spawning again.
        proc = subprocess.Popen([sys.executable, "-c", script])
        proc.wait(timeout=10)
        return proc.pid


if __name__ == "__main__":
    unittest.main()
