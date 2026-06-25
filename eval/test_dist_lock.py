"""Tests for dist_lock.py — Phase 5.3 distributed lock adapter.

Covers:
  * LockTimeout exception
  * InMemoryLock: acquire, release, re-entrant, timeout
  * FileLock: acquire, release, blocks other processes
  * NullLock: no-op counting
  * Factory: get_lock with each backend
  * locked() context manager
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from dist_lock import (  # noqa: E402
    FileLock,
    InMemoryLock,
    Lock,
    LockTimeout,
    NullLock,
    get_lock,
    locked,
)


class TestLockTimeout(unittest.TestCase):
    def test_is_exception(self):
        self.assertTrue(issubclass(LockTimeout, Exception))

    def test_message(self):
        try:
            raise LockTimeout("test message")
        except LockTimeout as e:
            self.assertIn("test message", str(e))


class TestInMemoryLock(unittest.TestCase):
    def test_acquire_release(self):
        lock = InMemoryLock()
        lock.acquire()
        lock.release()

    def test_protocol(self):
        lock = InMemoryLock()
        self.assertIsInstance(lock, Lock)

    def test_reentrant(self):
        lock = InMemoryLock()
        lock.acquire()
        lock.acquire()  # same process, no block
        lock.release()
        lock.release()

    def test_serializes_threads(self):
        """Two threads can't both hold the lock simultaneously."""
        lock = InMemoryLock()
        order: list[str] = []
        in_count = 0
        in_count_lock = threading.Lock()

        def worker(name: str):
            lock.acquire()
            nonlocal in_count
            with in_count_lock:
                in_count += 1
                self.assertEqual(in_count, 1, "lock not held exclusively")
            order.append(f"{name}-acquired")
            time.sleep(0.05)
            with in_count_lock:
                in_count -= 1
            order.append(f"{name}-released")
            lock.release()

        t1 = threading.Thread(target=worker, args=("a",))
        t2 = threading.Thread(target=worker, args=("b",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        # Both completed (order may vary, so just check both pairs).
        self.assertEqual(
            sorted(order),
            ["a-acquired", "a-released", "b-acquired", "b-released"],
        )

    def test_timeout_raises(self):
        lock = InMemoryLock()
        lock.acquire()

        # Second acquire with timeout=0.05 should raise.
        # Wait — re-entrant! The second acquire in the same thread
        # succeeds. We need a different thread.
        def try_acquire():
            with self.assertRaises(LockTimeout):
                lock.acquire(timeout=0.1)

        t = threading.Thread(target=try_acquire)
        t.start()
        t.join(timeout=2)
        lock.release()

    def test_idempotent_release(self):
        lock = InMemoryLock()
        lock.acquire()
        lock.release()
        # Releasing again is a no-op.
        lock.release()


class TestFileLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dist_lock_test_")
        self.path = Path(self.tmp) / "test.lock"

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_acquire_release(self):
        lock = FileLock(self.path, timeout=1.0)
        lock.acquire()
        self.assertTrue(self.path.exists())
        lock.release()

    def test_reentrant_in_same_process(self):
        lock = FileLock(self.path, timeout=1.0)
        lock.acquire()
        lock.acquire()  # same process: no block
        lock.release()
        lock.release()

    def test_serializes_processes(self):
        """FileLock serializes across processes — the actual production
        use case for POSIX flock.

        We use two subprocesses that each try to acquire the lock.
        Strict alternation in the log file proves serialization.
        """
        import subprocess

        flag = self.path.with_suffix(".flag")
        flag.write_text("")
        script = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(INSTALL_DIR)!r})\n"
            "from dist_lock import FileLock\n"
            f"lock = FileLock({str(self.path)!r}, timeout=10.0)\n"
            "lock.acquire()\n"
            f"open({str(flag)!r}, 'a').write('in\\n')\n"
            "time.sleep(0.3)\n"
            f"open({str(flag)!r}, 'a').write('out\\n')\n"
            "lock.release()\n"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for _ in range(2)
        ]
        for p in procs:
            p.wait(timeout=15)

        lines = [ln for ln in flag.read_text().splitlines() if ln]
        # Strict alternation: the second process can't enter until
        # the first has exited. Subprocess scheduling means we can't
        # predict which one starts first, but the order must
        # alternate: in, out, in, out.
        self.assertEqual(lines, ["in", "out", "in", "out"])

    def test_timeout_raises(self):
        """Hold the lock in one thread; another thread times out."""
        lock_holder = FileLock(self.path, timeout=1.0)
        lock_holder.acquire()
        try:
            lock_waiter = FileLock(self.path, timeout=0.2)
            with self.assertRaises(LockTimeout):
                lock_waiter.acquire()
        finally:
            lock_holder.release()

    def test_creates_parent_dir(self):
        nested = Path(self.tmp) / "a" / "b" / "c" / "test.lock"
        lock = FileLock(nested, timeout=1.0)
        lock.acquire()
        self.assertTrue(nested.exists())
        lock.release()

    def test_idempotent_release(self):
        lock = FileLock(self.path, timeout=1.0)
        lock.acquire()
        lock.release()
        lock.release()  # no error


class TestNullLock(unittest.TestCase):
    def test_counts(self):
        lock = NullLock()
        lock.acquire()
        lock.acquire()
        lock.release()
        self.assertEqual(lock.acquire_count, 2)
        self.assertEqual(lock.release_count, 1)

    def test_protocol(self):
        self.assertIsInstance(NullLock(), Lock)


class TestGetLock(unittest.TestCase):
    def test_memory_backend(self):
        lock = get_lock(backend="memory")
        self.assertIsInstance(lock, InMemoryLock)

    def test_null_backend(self):
        lock = get_lock(backend="null")
        self.assertIsInstance(lock, NullLock)

    def test_file_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.lock"
            lock = get_lock(backend="file", path=path, timeout=1.0)
            self.assertIsInstance(lock, FileLock)
            lock.acquire()
            lock.release()

    def test_auto_backend_on_posix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.lock"
            lock = get_lock(backend="auto", path=path, timeout=1.0)
            # On POSIX, auto → file.
            self.assertIsInstance(lock, FileLock)

    def test_unknown_backend_raises(self):
        with self.assertRaises(ValueError):
            get_lock(backend="redis")


class TestLockedContextManager(unittest.TestCase):
    def test_basic(self):
        lock = InMemoryLock()
        with locked(lock):
            pass
        # Released; we can re-acquire.
        lock.acquire()
        lock.release()

    def test_release_on_exception(self):
        lock = InMemoryLock()
        try:
            with locked(lock):
                raise RuntimeError("oops")
        except RuntimeError:
            pass
        # Lock should be released.
        lock.acquire()
        lock.release()

    def test_timeout_passed_through(self):
        """Timeout on the context manager propagates as LockTimeout.

        InMemoryLock is re-entrant per thread, so we need a *different*
        thread to attempt acquisition while the holder is in scope.
        Both threads must use the SAME lock instance.
        """
        shared_lock = InMemoryLock()
        shared_lock.acquire()  # main thread holds it
        try:
            result: dict = {}

            def try_acquire():
                try:
                    # Use the same instance, but in this thread.
                    with locked(shared_lock, timeout=0.1):
                        pass
                except LockTimeout as e:
                    result["raised"] = True
                    result["msg"] = str(e)

            t = threading.Thread(target=try_acquire)
            t.start()
            t.join(timeout=2)
            self.assertTrue(
                result.get("raised"),
                "LockTimeout not raised in other thread",
            )
            self.assertIn("could not acquire", result.get("msg", ""))
        finally:
            shared_lock.release()


if __name__ == "__main__":
    unittest.main()
