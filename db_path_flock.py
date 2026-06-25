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

    from db_path_flock import db_path_flock

    with db_path_flock(db_path):
        # Critical section.
        ...

Opt-out (rare)::

    from db_path_flock import is_db_path_flock_enabled
    if not is_db_path_flock_enabled():
        # Default-install path; flock is on.  Tests or debug only.
        ...
"""

from __future__ import annotations

__all__ = [
    "acquire_db_path_flock",
    "release_db_path_flock",
    "db_path_flock",
    "is_db_path_flock_enabled",
    "reset_db_path_flock_state",
]

import contextlib
import logging
import os
import threading
from pathlib import Path
from typing import IO, Optional, Protocol


class _SupportsClose(Protocol):
    """Anything with a close() method — satisfies the file-like protocol."""

    def close(self) -> None: ...


import contextlib
import logging
import os
import threading
from pathlib import Path
from typing import IO, Optional


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
    """Holds a single open file descriptor for the per-DB-path lock file.

    The fd is opened lazily on the first acquire and held open
    for the process's lifetime.  The ``acquire`` method
    short-circuits if the process already holds the flock (Linux
    flock is per-fd, so re-acquiring the same fd is a no-op).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._fd: Optional[IO[str]] = None
        self._ref_count = 0

    def _ensure_fd(self) -> IO[str]:
        """Open the lock file if not already open.

        The file is created next to the DB (``<dbname>.db.flock``).
        We create the parent dir if missing so brand-new memory
        dirs work.
        """
        if self._fd is not None:
            return self._fd
        lock_path = self.path.parent / f"{self.path.name}.flock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        return open(lock_path, "w")

    def acquire(self) -> None:
        """Acquire the flock.  Per-process re-entrant."""
        with self._lock:
            if self._ref_count == 0:
                self._fd = self._ensure_fd()
                from _lazy_imports import acquire_flock_with_retry

                try:
                    acquire_flock_with_retry(
                        self._fd,
                        max_attempts=10,
                        initial_backoff=0.05,
                        strict=True,
                    )
                except Exception:
                    # On failure, close the fd so a subsequent
                    # attempt can re-open it.
                    try:
                        self._fd.close()
                    except Exception:
                        pass
                    self._fd = None
                    raise
            self._ref_count += 1

    def release(self) -> None:
        """Release the flock.  Per-call — called once per
        ``db_path_flock`` exit.

        Releases the OS flock only when the reference count drops to 0.
        """
        with self._lock:
            if self._ref_count > 0:
                self._ref_count -= 1
                if self._ref_count == 0 and self._fd is not None:
                    try:
                        import fcntl as _fcntl
                        _fcntl.flock(self._fd.fileno(), _fcntl.LOCK_UN)
                    except Exception:
                        pass


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
    key = str(db_path)
    with _PATH_LOCKS_LOCK:
        lock = _PATH_LOCK_FDS.get(key)
        if lock is None:
            lock = PathLockFd(db_path)
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
