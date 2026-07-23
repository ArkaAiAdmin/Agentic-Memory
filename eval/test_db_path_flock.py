#!/usr/bin/env python3
"""Test the per-DB-path flock wrapper (default ON since 2026-06-22).

The default install enables ``MEMORY_DB_FLOCK`` for belt-and-suspenders
cross-process safety.  This test pins the contract: by default the
flock is acquired, but the env var can disable it.  The flock is
per-call (not per-process), so cron jobs can run between long-lived
processes' open_db calls.

Coverage:
    1. is_db_path_flock_enabled() defaults to True (2026-06-22
       follow-up: opt-OUT, not opt-in).
    2. ``MEMORY_DB_FLOCK=0`` disables the wrapper.
    3. acquire_db_path_flock acquires a lock file when enabled.
    4. acquire_db_path_flock is a no-op when disabled.
    5. Re-entrant: nested db_path_flock calls don't deadlock.
    6. release without prior acquire is a no-op (warning, not crash).
    7. Two threads in the same process share the lock (per-fd
       re-entrancy); they don't deadlock each other.
    8. End-to-end: open_db is automatically wrapped when enabled.
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from infra.db_path_flock import (  # noqa: E402
    acquire_db_path_flock,
    db_path_flock,
    is_db_path_flock_enabled,
    release_db_path_flock,
    reset_db_path_flock_state,
)


class TestDbPathFlockDefault(unittest.TestCase):
    """Default behaviour: ON, with opt-out via MEMORY_DB_FLOCK=0."""

    def setUp(self) -> None:
        # Save and clear the env var so the default applies.
        self._env_backup = os.environ.pop("MEMORY_DB_FLOCK", None)
        # Drop the in-process lock cache so the test sees a fresh
        # state for each run.
        reset_db_path_flock_state()

    def tearDown(self) -> None:
        if self._env_backup is not None:
            os.environ["MEMORY_DB_FLOCK"] = self._env_backup
        reset_db_path_flock_state()

    def test_default_is_enabled(self) -> None:
        """Default install: flock wrapper is ON.

        This is the post-2026-06-22 behaviour.  The audit gap
        was that cross-process safety relied on SQLite's
        busy_timeout alone; the flock wrapper is an explicit
        application-level serialisation that catches paths
        that bypass busy_timeout.
        """
        self.assertTrue(is_db_path_flock_enabled())

    def test_acquire_acquires_a_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.db"
            acquire_db_path_flock(db)
            try:
                lock_file = db.parent / f"{db.name}.flock"
                self.assertTrue(lock_file.exists())
            finally:
                release_db_path_flock(db)

    def test_context_manager_acquires_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.db"
            with db_path_flock(db):
                # Body runs while holding the lock.
                self.assertTrue((db.parent / f"{db.name}.flock").exists())
            # Lock file persists (fd is held for process lifetime)
            # but the OS-level flock is released on context exit.
            # The next acquire re-takes it cheaply.
            with db_path_flock(db):
                pass

    def test_reentrant_does_not_deadlock(self) -> None:
        """Same-process re-entrancy: nested calls don't deadlock."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.db"
            with db_path_flock(db):
                with db_path_flock(db):
                    with db_path_flock(db):
                        # Three levels of nesting — must not hang.
                        pass

    def test_two_threads_same_path_dont_deadlock(self) -> None:
        """Multiple threads serialize through PathLockFd without deadlock.

        The intra-process Condition in PathLockFd ensures threads take
        turns.  We inject a mock lock manager so the test doesn't depend
        on OS-level flock(2) behaviour across threads (unreliable on
        macOS with different fds to the same file).

        The mock patch is applied ONCE in the main thread (not per-worker)
        to avoid a concurrent ``unittest.mock.patch`` race condition where
        save/restore of the module attribute interleaves across threads
        and leaves the global ``get_lock_manager`` pointing at a MagicMock.
        """
        from infra.db_path_flock import _get_or_create_path_lock, reset_db_path_flock_state
        from infra.lock_manager import clear_lock_manager_cache
        from unittest.mock import MagicMock, patch

        mock_lm = MagicMock()
        mock_lm.acquire_lock.return_value = (True, "mock-token")
        mock_lm.release_lock.return_value = True

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.db"
            lock_fd = _get_or_create_path_lock(db)
            results: list[tuple[str, str]] = []
            errors: list[tuple[str, str]] = []

            def worker(tag: str) -> None:
                try:
                    lock_fd.acquire(timeout=10.0)
                    try:
                        time.sleep(0.01)
                        results.append((tag, "ok"))
                    finally:
                        lock_fd.release()
                except Exception as exc:
                    errors.append((tag, repr(exc)))

            # Patch applied once in main thread — safe from race.
            with patch("infra.lock_manager.get_lock_manager", return_value=mock_lm):
                try:
                    threads = [
                        threading.Thread(target=worker, args=(f"t{i}",)) for i in range(4)
                    ]
                    for t in threads:
                        t.start()
                    for t in threads:
                        t.join(timeout=15.0)

                    self.assertEqual(errors, [], f"Thread errors: {errors}")
                    self.assertEqual(len(results), 4)
                finally:
                    reset_db_path_flock_state()
                    # Ensure the global lock manager singleton is
                    # recreated fresh for subsequent tests.
                    clear_lock_manager_cache()

    def test_release_without_acquire_is_noop(self) -> None:
        """No prior acquire: release is a no-op (debug log, no crash)."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.db"
            release_db_path_flock(db)  # no exception


class TestDbPathFlockDisabled(unittest.TestCase):
    """With MEMORY_DB_FLOCK=0, the wrapper is a no-op."""

    def setUp(self) -> None:
        self._env_backup = os.environ.get("MEMORY_DB_FLOCK")
        os.environ["MEMORY_DB_FLOCK"] = "0"
        reset_db_path_flock_state()

    def tearDown(self) -> None:
        if self._env_backup is None:
            os.environ.pop("MEMORY_DB_FLOCK", None)
        else:
            os.environ["MEMORY_DB_FLOCK"] = self._env_backup
        reset_db_path_flock_state()

    def test_zero_disables(self) -> None:
        self.assertFalse(is_db_path_flock_enabled())

    def test_acquire_is_noop_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.db"
            acquire_db_path_flock(db)
            # No lock file created.
            self.assertFalse((db.parent / f"{db.name}.flock").exists())

    def test_context_manager_is_noop_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.db"
            with db_path_flock(db):
                # Body runs without any lock.
                self.assertFalse((db.parent / f"{db.name}.flock").exists())


class TestDisableValues(unittest.TestCase):
    """The disable values are 0/false/no/off (case-insensitive)."""

    def setUp(self) -> None:
        self._env_backup = os.environ.get("MEMORY_DB_FLOCK")
        os.environ.pop("MEMORY_DB_FLOCK", None)
        reset_db_path_flock_state()

    def tearDown(self) -> None:
        if self._env_backup is not None:
            os.environ["MEMORY_DB_FLOCK"] = self._env_backup
        reset_db_path_flock_state()

    def test_false(self) -> None:
        os.environ["MEMORY_DB_FLOCK"] = "false"
        self.assertFalse(is_db_path_flock_enabled())

    def test_no(self) -> None:
        os.environ["MEMORY_DB_FLOCK"] = "no"
        self.assertFalse(is_db_path_flock_enabled())

    def test_off(self) -> None:
        os.environ["MEMORY_DB_FLOCK"] = "off"
        self.assertFalse(is_db_path_flock_enabled())

    def test_disable(self) -> None:
        os.environ["MEMORY_DB_FLOCK"] = "disable"
        self.assertFalse(is_db_path_flock_enabled())

    def test_empty_string_uses_default(self) -> None:
        os.environ["MEMORY_DB_FLOCK"] = ""
        self.assertTrue(is_db_path_flock_enabled())

    def test_arbitrary_string_uses_default(self) -> None:
        os.environ["MEMORY_DB_FLOCK"] = "maybe"
        # "maybe" isn't a known disable value, so we default to ON.
        # The contract is: explicit disable = one of the known
        # values; everything else = ON.
        self.assertTrue(is_db_path_flock_enabled())


class TestOpenDbWiring(unittest.TestCase):
    """open_db() automatically acquires the flock when enabled."""

    def setUp(self) -> None:
        self._env_backup = os.environ.get("MEMORY_DB_FLOCK")
        os.environ.pop("MEMORY_DB_FLOCK", None)
        reset_db_path_flock_state()

    def tearDown(self) -> None:
        if self._env_backup is not None:
            os.environ["MEMORY_DB_FLOCK"] = self._env_backup
        reset_db_path_flock_state()

    def test_open_db_creates_lock_file(self) -> None:
        """By default, open_db acquires the flock and creates the
        lock file next to the DB."""
        from infra.db import open_db

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.db"
            with open_db(db) as conn:
                n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                self.assertEqual(n, 0)
            # Lock file created during the open_db call.
            self.assertTrue((db.parent / f"{db.name}.flock").exists())

    def test_open_db_skips_lock_when_disabled(self) -> None:
        from infra.db import open_db

        os.environ["MEMORY_DB_FLOCK"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.db"
            with open_db(db):
                pass
            self.assertFalse((db.parent / f"{db.name}.flock").exists())


if __name__ == "__main__":
    unittest.main()
