"""Distributed lock adapter (Phase 5.3).

Abstracts the locking mechanism so call sites can pick the right
implementation for their deployment:

  * ``FileLock`` — POSIX flock, the default for single-host deploys
  * ``InMemoryLock`` — in-process lock for tests; cannot coordinate
    across processes
  * ``NullLock`` — no-op for benchmarks; the saga runs without
    coordination (DANGEROUS in production, but useful for measuring
    the lock-free path)

Future (not yet implemented):
  * ``RedisLock`` — for multi-host deployments
  * ``EtcdLock`` / ``ZookeeperLock`` — for stronger consistency

The default install uses ``FileLock`` via ``db_path_flock.py``.
This module is the *contract* for future distributed deployments.

Why this matters
----------------

The agentic-memory saga serialises writes through a single
``db_path_flock`` per DB path. That's the right model for a
single-host install with multiple long-lived processes
(opencode, auto_save.py daemon, background_worker, cron scripts).
For a multi-host install (e.g., two opencode agents sharing one
remote DB) we need a lock that survives a network round-trip.
The protocol here is the surface those implementations will
implement.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Lock(Protocol):
    """A cross-process / cross-host lock.

    Implementations:
      * FileLock — POSIX flock
      * InMemoryLock — single-process, for tests
      * NullLock — no-op, for benchmarks

    Usage:
        with lock.acquire(timeout=5.0):
            # critical section
    """

    name: str

    def acquire(self, timeout: float = 30.0) -> None:
        """Block until the lock is held or timeout expires.

        Raises ``LockTimeout`` on timeout.
        """
        ...

    def release(self) -> None:
        """Release the lock. Idempotent: releasing twice is a no-op."""
        ...


class LockTimeout(Exception):
    """Raised when ``acquire`` exceeds its timeout."""


# ---------------------------------------------------------------------------
# In-memory implementation (tests, single-process)
# ---------------------------------------------------------------------------


class InMemoryLock:
    """In-process lock — fast, no filesystem I/O, no cross-process safety.

    Use this for:
      * Unit tests that exercise saga ordering without touching disk
      * Single-process benchmarks
      * Anywhere cross-process coordination is handled elsewhere

    NOT safe for production. Multiple processes / threads running
    the same code path can both acquire the "lock" simultaneously.

    Re-entrant in the same thread: a thread that already holds
    the lock can acquire it again without blocking. (This matches
    POSIX flock's per-fd behavior.)
    """

    def __init__(self, name: str = "in-memory") -> None:
        self.name = name
        self._lock = threading.Lock()
        self._owner: int | None = None
        self._depth = 0

    def acquire(self, timeout: float = 30.0) -> None:
        tid = threading.get_ident()
        if self._owner == tid:
            # Re-entrant: same thread, just bump depth.
            self._depth += 1
            return
        if not self._lock.acquire(blocking=True, timeout=timeout):
            raise LockTimeout(f"could not acquire {self.name} in {timeout}s")
        self._owner = tid
        self._depth = 1

    def release(self) -> None:
        if self._owner is None:
            return
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()


# ---------------------------------------------------------------------------
# File-based lock (POSIX flock) — the default
# ---------------------------------------------------------------------------


class FileLock:
    """POSIX flock-based lock, the default for single-host deploys.

    Per-process re-entrant: the same process can acquire the lock
    multiple times without blocking (Linux flock is per-fd).

    Cross-process serialisation: blocks until the file is unlocked
    by the holder, or the timeout expires.

    Args:
        path: Path to the lock file. Created if missing.
        timeout: Seconds to wait for acquisition. Raises
            LockTimeout on timeout.
    """

    def __init__(self, path: str | Path, timeout: float = 30.0) -> None:
        self.path = Path(path)
        self.name = f"file:{self.path}"
        self.timeout = timeout
        self._fd: int | None = None
        self._lock = threading.Lock()
        self._reentrant_count = 0

    def acquire(self, timeout: float | None = None) -> None:
        if timeout is None:
            timeout = self.timeout
        with self._lock:
            if self._fd is not None:
                # Re-entrant in this process.
                self._reentrant_count += 1
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Open in append mode so writes (if any) don't truncate.
            self._fd = os.open(
                str(self.path),
                os.O_CREAT | os.O_RDWR | os.O_APPEND,
                0o644,
            )
        # Acquire flock outside the lock so we don't hold the
        # threading lock during the (potentially long) blocking call.
        start = time.monotonic()
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                elapsed = time.monotonic() - start
                if elapsed >= timeout:
                    os.close(self._fd)
                    self._fd = None
                    raise LockTimeout(f"could not acquire {self.name} in {timeout}s")
                # Short backoff before retry.
                time.sleep(0.05)
        with self._lock:
            self._reentrant_count = 1

    def release(self) -> None:
        with self._lock:
            if self._reentrant_count > 1:
                self._reentrant_count -= 1
                return
            if self._fd is None:
                return
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError as e:
                LOG.warning("FileLock release on %s: %s", self.path, e)
            finally:
                os.close(self._fd)
                self._fd = None
                self._reentrant_count = 0


# ---------------------------------------------------------------------------
# Null lock (benchmarks only)
# ---------------------------------------------------------------------------


class NullLock:
    """A lock that does nothing.

    Useful for benchmarks measuring the lock-free path. NEVER
    safe in production — multiple processes can simultaneously
    hold the "lock", which is the bug the lock exists to prevent.
    """

    def __init__(self) -> None:
        self.name = "null"
        self.acquire_count = 0
        self.release_count = 0

    def acquire(self, timeout: float = 30.0) -> None:
        self.acquire_count += 1

    def release(self) -> None:
        self.release_count += 1


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_lock(
    backend: str = "auto",
    path: str | Path | None = None,
    timeout: float = 30.0,
) -> Lock:
    """Factory for locks.

    Args:
        backend: ``"auto"`` (FileLock on POSIX, InMemoryLock else),
            ``"file"``, ``"memory"``, ``"null"``.
        path: Lock file path. Required for ``"file"``, ignored for
            the others. If None, defaults to ``/tmp/agentic-memory-<uuid>.lock``.
        timeout: Acquisition timeout in seconds.

    Returns:
        A Lock instance.
    """
    if backend == "auto":
        if hasattr(fcntl, "flock"):
            backend = "file"
        else:
            backend = "memory"

    if backend == "file":
        if path is None:
            path = Path(f"/tmp/agentic-memory-{uuid.uuid4().hex}.lock")
        return FileLock(path, timeout=timeout)
    if backend == "memory":
        return InMemoryLock()
    if backend == "null":
        return NullLock()
    raise ValueError(
        f"unknown lock backend: {backend!r}. Valid: 'auto', 'file', 'memory', 'null'"
    )


@contextlib.contextmanager
def locked(lock: Lock, timeout: float = 30.0) -> Iterator[None]:
    """Context manager wrapper around a Lock.

    Usage:
        with locked(lock):
            ...
    """
    lock.acquire(timeout=timeout)
    try:
        yield
    finally:
        lock.release()


__all__ = [
    "FileLock",
    "InMemoryLock",
    "Lock",
    "LockTimeout",
    "NullLock",
    "get_lock",
    "locked",
]
