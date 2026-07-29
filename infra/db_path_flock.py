"""Per-DB-path flock wrapper for cross-process write safety.

Belt-and-suspenders layer above SQLite's WAL + busy_timeout.  When
enabled (the default since 2026-06-22 follow-up), every
``db_path_flock()`` context acquire+releases a per-DB-path ``flock``
that serialises writes across processes at the application layer.

When to use this
----------------

This wrapper is enabled by default (``MEMORY_DB_FLOCK=1``).  The
default install should be safe; the only reason to disable it
(``MEMORY_DB_FLOCK=0``) is to debug a suspected flock-related
issue or to opt out of the cross-process safety net for an exotic
deployment that handles serialisation elsewhere.

Why it's enabled by default
---------------------------

The default install has multiple processes that write to the same
DB (the opencode session, ``auto_save.py daemon``,
``background_worker``, and 25+ cron scripts).  Without this
wrapper, safety relies entirely on SQLite's WAL + busy_timeout
(see ``eval/test_cross_process_safety.py`` for the proof that
this works).  The flock wrapper is an explicit, application-level
serialisation that catches any path that bypasses SQLite's
busy_timeout (e.g., a third-party SQLite extension that ignores
the PRAGMA).

How it integrates with the rest of the system
---------------------------------------------

``open_db()`` in ``db.py`` automatically wraps each connection in
a ``db_path_flock()`` block.  Every caller of
``connection_pool.get()`` (35+ call sites) is therefore protected
without per-caller changes.  The ``safe_close_db()`` path goes
through ``open_db``'s context manager, so the lock is released
on close.

Design
------

* **Per-call, not per-process.**  Earlier versions of this module
  held the flock for the process's lifetime, which would have
  blocked every cron job for the duration of the opencode
  session.  The current design acquires on ``__enter__`` and
  releases on ``__exit__``.

* **Re-entrant within a process.**  Linux's ``flock`` is per-fd;
  the same fd can be acquired multiple times by the same process
  without blocking.  We exploit this to support nested
  ``db_path_flock`` calls (e.g., a tool that opens a conn while
  another conn is already open in the same context).

* **File descriptor cached.**  Opening the lock file on every
  acquire is wasteful; the fd is cached per-DB-path for the
  process's lifetime.  The cache is keyed by ``str(path)`` so
  each unique DB path has at most one open fd.

* **Default ON.**  ``MEMORY_DB_FLOCK=0`` to disable.  ``=1``,
  ``=true``, ``=yes`` to enable (the default).

Usage
-----

The wrapper is automatic — every ``open_db()`` call is protected.
Manual opt-in::

    from infra.db_path_flock import db_path_flock

    with db_path_flock(db_path):
        # Critical section.
        ...

Opt-out (rare)::

    from infra.db_path_flock import is_db_path_flock_enabled
    if not is_db_path_flock_enabled():
        # Default-install path; flock is on.  Tests or debug only.
        ...
"""

from __future__ import annotations

import logging

__all__ = [
    "acquire_db_path_flock",
    "release_db_path_flock",
    "db_path_flock",
    "is_db_path_flock_enabled",
    "reset_db_path_flock_state",
]

import contextlib
import os
import threading
from pathlib import Path
from typing import Protocol



logger = logging.getLogger(__name__)


# Per-DB-path lock files.  Keyed by str(path).  The lock_file fd
# is held for the process's lifetime so we don't re-open the file
# on every acquire.  Linux's flock is per-fd: the same fd can be
# acquired multiple times by the same process without blocking,
# so re-entrant ``db_path_flock(path)`` calls within one process
# are safe.
_PATH_LOCK_FDS: dict[str, "PathLockFd"] = {}
_PATH_LOCKS_LOCK = threading.Lock()


class PathLockFd:
    """Holds a lock lease for the per-DB-path database lock.

    The lock is acquired dynamically using get_lock_manager().
    Per-thread re-entrant: a thread that already holds the lock can
    acquire it again (depth counting).  Different threads within the
    same process are serialised via an intra-process lock.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cond = threading.Condition()
        self._owner: int | None = None
        self._depth = 0
        self._lease_token: str | None = None

    def acquire(self, timeout: float = 30.0) -> None:
        """Acquire the lock. Per-thread re-entrant.

        Blocks by polling get_lock_manager() until the lock is acquired
        or *timeout* seconds elapse. Raises ``TimeoutError`` on timeout.
        Different threads are serialised: a thread that doesn't own the
        lock blocks on the intra-process condition variable until the owner fully
        releases.
        """
        tid = threading.get_ident()
        import os
        import time
        from infra.lock_manager import get_lock_manager

        deadline = time.monotonic() + timeout

        with self._cond:
            if self._owner == tid:
                # Re-entrant: same thread, just bump depth.
                self._depth += 1
                return

            # Wait until no other thread in this process owns the lock
            while self._owner is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Could not acquire db_path_flock for {self.path} within {timeout}s"
                    )
                self._cond.wait(timeout=min(remaining, 0.5))

            # Claim intra-process ownership while acquiring inter-process lock
            self._owner = tid
            self._depth = 1

        # Now poll inter-process lease without holding _cond so other threads can inspect/release if needed
        lm = get_lock_manager()
        key = os.path.abspath(str(self.path))
        backoff = 0.05
        acquired = False
        try:
            while True:
                success, token = lm.acquire_lock(key, "db-path-flock", ttl_seconds=300)
                if success:
                    with self._cond:
                        self._lease_token = token
                    acquired = True
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Could not acquire db_path_flock for {self.path} within {timeout}s"
                    )
                time.sleep(min(backoff, deadline - time.monotonic()))
                backoff = min(backoff * 2, 1.0)
        finally:
            if not acquired:
                with self._cond:
                    self._owner = None
                    self._depth = 0
                    self._cond.notify_all()

    def release(self) -> None:
        """Release the lock lease when reference count reaches 0."""
        tid = threading.get_ident()
        lease_to_release = None
        with self._cond:
            if self._owner != tid:
                return
            self._depth -= 1
            if self._depth > 0:
                return

            lease_to_release = self._lease_token
            self._lease_token = None
            self._owner = None
            self._cond.notify_all()

        if lease_to_release is not None:
            import os
            from infra.lock_manager import get_lock_manager
            lm = get_lock_manager()
            key = os.path.abspath(str(self.path))
            try:
                lm.release_lock(key, lease_to_release)
            except Exception as e:
                logger.warning("db_path_flock release failed: %s", e)


def is_db_path_flock_enabled() -> bool:
    """True if the cross-process flock wrapper is enabled.

    Default: **enabled** (returns True unless ``MEMORY_DB_FLOCK``
    is set to one of the explicit-disable values).  Operators
    can opt out by setting ``MEMORY_DB_FLOCK=0`` (or ``=false``,
    ``=no``, ``=off``).
    """
    val = os.environ.get("MEMORY_DB_FLOCK", "").strip().lower()
    if val in ("0", "false", "no", "off", "disable", "disabled"):
        return False
    # Default is enabled.
    return True


def _get_or_create_path_lock(db_path: Path) -> PathLockFd:
    """Get-or-create the per-DB-path PathLockFd."""
    resolved_path = Path(db_path).resolve()
    key = str(resolved_path)
    with _PATH_LOCKS_LOCK:
        lock = _PATH_LOCK_FDS.get(key)
        if lock is None:
            lock = PathLockFd(resolved_path)
            _PATH_LOCK_FDS[key] = lock
        return lock


def acquire_db_path_flock(db_path: Path) -> None:
    """Acquire the per-DB-path flock (per-call, not per-process).

    Re-entrant within a process: a second acquire on the same
    path from the same process is a no-op.  The caller is
    responsible for pairing with ``release_db_path_flock``.

    When the env var ``MEMORY_DB_FLOCK=0``, this is a no-op.
    """
    if not is_db_path_flock_enabled():
        return
    _get_or_create_path_lock(db_path).acquire()


def release_db_path_flock(db_path: Path) -> None:
    """Release a per-DB-path flock acquired via ``acquire_db_path_flock``.

    When the env var ``MEMORY_DB_FLOCK=0``, this is a no-op.
    """
    if not is_db_path_flock_enabled():
        return
    key = str(db_path)
    with _PATH_LOCKS_LOCK:
        lock = _PATH_LOCK_FDS.get(key)
    if lock is None:
        # No prior acquire — this is a no-op (the caller probably
        # took a fast path that bypassed the wrapper).  Log a
        # warning so the mismatch surfaces in debugging.
        logger.debug(
            "release_db_path_flock(%s) called without prior acquire",
            db_path,
        )
        return
    lock.release()


@contextlib.contextmanager
def db_path_flock(db_path: Path):
    """Context manager: acquire + release a per-DB-path flock.

    When the env var ``MEMORY_DB_FLOCK=0``, the body runs without
    any locking.  Otherwise the body runs while holding a
    per-process flock on ``<dbpath>.db.flock`` — concurrent
    processes block on the same flock for the duration of the
    body.

    Re-entrant: nesting ``db_path_flock(path)`` inside
    ``db_path_flock(path)`` is a no-op for the inner acquire
    (Linux flock is per-fd; the inner acquisition is a no-op
    since the outer already holds it).
    """
    enabled = is_db_path_flock_enabled()
    if enabled:
        acquire_db_path_flock(db_path)
    try:
        yield
    finally:
        if enabled:
            release_db_path_flock(db_path)


def reset_db_path_flock_state() -> None:
    """Test helper: clear the per-DB-path lock cache.

    This does NOT release the OS-level flock (the fds stay open
    until process exit).  It just drops the in-process cache
    so a subsequent ``_get_or_create_path_lock`` creates a
    fresh ``PathLockFd`` pointing at the same fd.  Useful for
    tests that need to reset state between subtests.
    """
    global _PATH_LOCK_FDS
    with _PATH_LOCKS_LOCK:
        _PATH_LOCK_FDS = {}
