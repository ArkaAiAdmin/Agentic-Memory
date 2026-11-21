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
    lock_fd,
    unlock_fd,
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
            # The lock manager uses <path.parent> / .rebuild.lock as the actual lock file.
            lock_path = db_path.parent / ".rebuild.lock"
            # Hold the flock in a subprocess on the SAME file the lock manager uses.
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


class TestLockFdAndUnlockFd(unittest.TestCase):
    def test_lock_fd_unlock_fd_basic(self):
        f1 = tempfile.NamedTemporaryFile(delete=False)
        f2 = open(f1.name, "w")
        try:
            self.assertTrue(lock_fd(f1.fileno(), nonblocking=True))
            self.assertFalse(lock_fd(f2.fileno(), nonblocking=True))
            unlock_fd(f1.fileno())
            self.assertTrue(lock_fd(f2.fileno(), nonblocking=True))
            unlock_fd(f2.fileno())
        finally:
            f1.close()
            f2.close()
            os.unlink(f1.name)

    def test_lock_fd_win32_mocked(self):
        import types
        from unittest import mock
        import infra.file_lock as fl

        calls = []

        class FakeK32:
            def __init__(self, lock_rv=1, unlock_rv=1):
                self.lock_rv = lock_rv
                self.unlock_rv = unlock_rv

            def LockFileEx(self, handle, flags, reserved, low, high, overlapped):
                calls.append(("LockFileEx", flags, low, high))
                return self.lock_rv

            def UnlockFileEx(self, handle, reserved, low, high, overlapped):
                calls.append(("UnlockFileEx", reserved, low, high))
                return self.unlock_rv

        fake_msvcrt = types.SimpleNamespace(get_osfhandle=lambda fd: 0x1234)
        fake_k32 = FakeK32(lock_rv=1, unlock_rv=1)

        with (
            mock.patch.object(fl, "_fcntl", None),
            mock.patch.object(fl.sys, "platform", "win32"),
            mock.patch.object(fl, "_get_kernel32", return_value=fake_k32),
            mock.patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
        ):
            self.assertTrue(fl.lock_fd(42, nonblocking=True))
            self.assertEqual(calls[-1], ("LockFileEx", 0x2 | 0x1, 1, 0))
            fl.unlock_fd(42)
            self.assertEqual(calls[-1], ("UnlockFileEx", 0, 1, 0))

            # Contention: ERROR_LOCK_VIOLATION (33) returns False when nonblocking=True
            fake_k32.lock_rv = 0
            with mock.patch.object(fl.ctypes, "get_last_error", return_value=33, create=True):
                self.assertFalse(fl.lock_fd(42, nonblocking=True))
                with self.assertRaises(OSError):
                    fl.lock_fd(42, nonblocking=False)

            # Unexpected Win32 error raises OSError
            with mock.patch.object(fl.ctypes, "get_last_error", return_value=5, create=True):
                with self.assertRaises(OSError):
                    fl.lock_fd(42, nonblocking=True)

    def test_is_stale_lock_win32_avoids_fuser_lsof(self):
        from unittest import mock
        import infra.file_lock as fl

        with tempfile.NamedTemporaryFile(delete=False) as f:
            lock_path = Path(f.name)
        try:
            with (
                mock.patch.object(fl.sys, "platform", "win32"),
                mock.patch.object(fl.subprocess, "run", side_effect=AssertionError("must not invoke fuser/lsof")),
                mock.patch.object(fl, "lock_fd", return_value=True),
                mock.patch.object(fl, "unlock_fd"),
            ):
                self.assertTrue(fl._is_stale_lock(lock_path))

            with (
                mock.patch.object(fl.sys, "platform", "win32"),
                mock.patch.object(fl.subprocess, "run", side_effect=AssertionError("must not invoke fuser/lsof")),
                mock.patch.object(fl, "lock_fd", return_value=False),
            ):
                self.assertFalse(fl._is_stale_lock(lock_path))
        finally:
            lock_path.unlink(missing_ok=True)


class TestFlockLockManager(unittest.TestCase):
    def test_lock_file_handles_posix_and_windows_separators(self):
        from infra.lock_manager import FlockLockManager

        mgr = FlockLockManager("/tmp/base/memory.db")
        self.assertEqual(mgr._lock_file("/tmp/a/memory.db"), "/tmp/a/memory.db.flock")
        self.assertEqual(mgr._lock_file("C:\\Users\\mem\\memory.db"), "C:\\Users\\mem\\memory.db.flock")
        hashed = mgr._lock_file("plain_lock_name")
        self.assertTrue(hashed.startswith("/tmp/base/memory.db.flock."))

    def test_acquire_reentrant_refcount_and_release(self):
        from infra.lock_manager import FlockLockManager

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.db"
            mgr = FlockLockManager(db_path)
            lock_key = str(db_path)

            ok1, tok1 = mgr.acquire_lock(lock_key, "holder1")
            self.assertTrue(ok1)
            self.assertTrue(tok1)

            # Same-process re-entrant acquire reuses cached fd and yields new token
            ok2, tok2 = mgr.acquire_lock(lock_key, "holder2")
            self.assertTrue(ok2)
            self.assertNotEqual(tok1, tok2)
            self.assertTrue(mgr.is_locked(lock_key))

            # Unknown token fails safely
            self.assertFalse(mgr.release_lock(lock_key, "wrong-token"))

            # Releasing first token keeps lock held
            self.assertTrue(mgr.release_lock(lock_key, tok1))
            self.assertTrue(mgr.is_locked(lock_key))

            # Separate FlockLockManager instance sees contention
            other_mgr = FlockLockManager(db_path)
            ok_other, tok_other = other_mgr.acquire_lock(lock_key, "other")
            self.assertFalse(ok_other)
            self.assertEqual(tok_other, "")

            # Releasing final token unlocks and closes fd
            self.assertTrue(mgr.release_lock(lock_key, tok2))
            self.assertFalse(mgr.is_locked(lock_key))


class TestRunSchemaSetupForeignKeysRestore(unittest.TestCase):
    def test_restores_foreign_keys_on_error_fresh_db(self):
        import sqlite3
        from unittest import mock
        import infra.db_migrations as dm

        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with mock.patch.object(
                dm, "_migrate_ensure_columns", side_effect=RuntimeError("boom")
            ):
                with self.assertRaises(RuntimeError):
                    dm.run_schema_setup(conn)
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            self.assertEqual(fk, 1)
        finally:
            conn.close()

    def test_restores_foreign_keys_on_error_behind_schema(self):
        import sqlite3
        from unittest import mock
        import infra.db_migrations as dm

        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("CREATE TABLE schema_version (id INTEGER PRIMARY KEY, version INTEGER)")
        conn.execute("INSERT INTO schema_version (id, version) VALUES (1, 1)")
        conn.commit()
        try:
            with mock.patch.object(
                dm, "_migrate_memory_audit_log", side_effect=RuntimeError("boom2")
            ):
                with self.assertRaises(RuntimeError):
                    dm.run_schema_setup(conn)
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            self.assertEqual(fk, 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
