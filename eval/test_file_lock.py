#!/usr/bin/env python3
"""Unit tests for file_lock.py.

Run with:
    ~/.config/agentic-memory/venv/bin/python eval/test_file_lock.py
"""

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
import subprocess

from infra.file_lock import (
    acquire_flock_with_retry,
    release_flock,
    FileLockError,
    _try_flock,
)


def _can_fcntl():
    try:
        import fcntl  # noqa: F401

        return True
    except ImportError:
        return False


class TestTryFlock(unittest.TestCase):
    def test_try_flock_success(self):
        f = tempfile.NamedTemporaryFile(delete=False)
        try:
            result = _try_flock(f, nonblocking=True)
            self.assertTrue(result)
        finally:
            f.close()
            os.unlink(f.name)

    def test_try_flock_locked(self):
        f1 = tempfile.NamedTemporaryFile(delete=False)
        f2 = open(f1.name, "w")
        try:
            _try_flock(f1, nonblocking=True)
            result = _try_flock(f2, nonblocking=True)
            self.assertFalse(result)
        finally:
            f1.close()
            f2.close()
            os.unlink(f1.name)


class TestAcquireFlock(unittest.TestCase):
    def test_acquire_and_release(self):
        f = tempfile.NamedTemporaryFile(delete=False)
        try:
            ok = acquire_flock_with_retry(f, max_attempts=3, nonblocking=True)
            self.assertTrue(ok)
            release_flock(f)
        finally:
            if not f.closed:
                f.close()
            os.unlink(f.name)

    def test_acquire_twice_fails_nonblocking(self):
        f = tempfile.NamedTemporaryFile(delete=False)
        f2 = open(f.name, "w")
        try:
            acquire_flock_with_retry(f, max_attempts=1, nonblocking=True)
            ok = acquire_flock_with_retry(
                f2, max_attempts=1, nonblocking=True, strict=False
            )
            self.assertFalse(ok)
        finally:
            release_flock(f)
            f2.close()
            f.close()
            os.unlink(f.name)

    def test_strict_raises(self):
        f = tempfile.NamedTemporaryFile(delete=False)
        f2 = open(f.name, "w")
        try:
            acquire_flock_with_retry(f, max_attempts=1, nonblocking=True)
            with self.assertRaises(FileLockError):
                acquire_flock_with_retry(
                    f2, max_attempts=1, nonblocking=True, strict=True
                )
        finally:
            release_flock(f)
            f2.close()
            f.close()
            os.unlink(f.name)

    def test_release_unlocked_file_is_safe(self):
        ok = release_flock(None)
        self.assertFalse(ok)

    def test_concurrent_lock_contention_same_process(self):
        f = tempfile.NamedTemporaryFile(delete=False)
        f2 = open(f.name, "w")
        results = []

        def lock_and_record():
            ok = acquire_flock_with_retry(
                f2, max_attempts=5, initial_backoff=0.01, nonblocking=True, strict=False
            )
            results.append(ok)

        try:
            acquire_flock_with_retry(f, max_attempts=1, nonblocking=True)
            t = threading.Thread(target=lock_and_record)
            t.start()
            t.join(timeout=5)
            self.assertFalse(results[0])
        finally:
            release_flock(f)
            f2.close()
            f.close()
            os.unlink(f.name)


class TestReleaseFlock(unittest.TestCase):
    def test_release_closes_file(self):
        f = tempfile.NamedTemporaryFile(delete=False)
        acquire_flock_with_retry(f, max_attempts=1, nonblocking=True)
        release_flock(f)
        self.assertTrue(f.closed)

    def test_release_twice_is_safe(self):
        f = tempfile.NamedTemporaryFile(delete=False)
        acquire_flock_with_retry(f, max_attempts=1, nonblocking=True)
        release_flock(f)
        release_flock(f)


# ===========================================================================
# P0-5 regression: save_pipeline._acquire_lock must surface FileLockError
# ===========================================================================


class TestSavePipelineAcquireLock(unittest.TestCase):
    """P0-5 regression (2026-06-22): strict=True contract must be honored.

    Before the fix, save_pipeline._acquire_lock caught FileLockError
    and returned None, silently defeating the strict-mode contract.
    The fix: FileLockError now propagates from _acquire_lock so
    callers can decide whether to retry, fall back, or surface the
    error.  Non-lock exceptions (e.g. OSError opening the file)
    still return None because they're infrastructure errors, not
    contention errors.
    """

    def test_strict_raises_on_contention(self):
        """_acquire_lock must raise FileLockError when the flock is held."""
        from save_pipeline import _acquire_lock

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db_path.touch()
            # The lock manager uses <path>.flock as the actual lock file.
            lock_path = db_path.parent / ".rebuild.lock.flock"
            # Hold the flock in a subprocess on the SAME file the lock manager uses.
            import fcntl as _fcntl
            script = (
                "import fcntl, os, time, sys; "
                "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o644); "
                "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); "
                "time.sleep(10)"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", script, str(lock_path)],
                close_fds=True,
            )
            try:
                import time as _time
                _time.sleep(0.3)
                os.environ["MEMORY_DB_LOCK_ATTEMPTS"] = "2"
                try:
                    with self.assertRaises(FileLockError):
                        _acquire_lock(db_path)
                finally:
                    os.environ.pop("MEMORY_DB_LOCK_ATTEMPTS", None)
            finally:
                proc.terminate()
                proc.wait()
                lock_path.unlink(missing_ok=True)

    def test_returns_lock_file_on_success(self):
        """_acquire_lock must return the lock_file when no contention."""
        from save_pipeline import _acquire_lock
        from infra.file_lock import release_flock

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db_path.touch()
            try:
                lock_file = _acquire_lock(db_path)
                self.assertIsNotNone(lock_file)
            finally:
                if lock_file is not None:
                    try:
                        release_flock(lock_file)
                    except Exception:
                        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
