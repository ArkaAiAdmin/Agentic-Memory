"""File-locking helpers (flock with bounded retry).

Extracted from memory_common.py during the 6-module refactor.

Provides:
  * ``acquire_flock_with_retry``: flock with exponential backoff.
  * ``release_flock``: best-effort unlock + close.
  * ``_try_flock`` (private): single flock attempt.
"""

from __future__ import annotations

import logging
logger = logging.getLogger(__name__)

import subprocess
import time


__all__ = ["acquire_flock_with_retry", "release_flock", "FileLockError"]


class FileLockError(RuntimeError):
    """Raised by ``acquire_flock_with_retry(strict=True)`` after retries exhausted.

    P1-15 fix: callers on the write path (save_memory, atomic_write of
    the MEMORY.md index) must not silently proceed without the lock —
    two concurrent saves can otherwise both write and leave the
    ``memory_vec_keys`` and the markdown index drift. The
    ``strict=True`` mode makes the contract explicit; non-critical
    callers (read paths, audit log appends) keep the old best-effort
    behaviour via ``strict=False`` (the default).
    """


def _try_flock(lock_file, nonblocking: bool) -> bool:
    """Single flock attempt using the pluggable LockManager.

    Uses os.path.abspath(lock_file.name) as the lock key.
    """
    if lock_file is None or not hasattr(lock_file, "name"):
        return False
    import os
    from infra.lock_manager import get_lock_manager
    lm = get_lock_manager()
    key = os.path.abspath(lock_file.name)
    success, token = lm.acquire_lock(key, "file-lock", ttl_seconds=300)
    if success:
        setattr(lock_file, "_lease_token", token)
        return True
    return False


def _is_stale_lock(lock_path) -> bool:
    """Return True if the lock file exists on disk but no live process holds a flock on it.

    Uses ``fuser`` to detect open FDs; falls back to ``lsof``. Neither
    command finding an FD means either the OS released the flock (process
    died) or the file is simply not open — both are stale.
    """
    if not lock_path.exists():
        return False
    try:
        for cmd in (["fuser", str(lock_path)], ["lsof", "-t", str(lock_path)]):
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
                pids = [p.strip() for p in out.stdout.split() if p.strip().isdigit()]
                if pids:
                    return False
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        return True
    except Exception as e:
        logger.warning("_is_stale_lock failed: %s", e)
        return False


def acquire_flock_with_retry(
    lock_file,
    max_attempts: int = 5,
    initial_backoff: float = 0.05,
    backoff_multiplier: float = 2.0,
    max_backoff: float = 1.0,
    nonblocking: bool = True,
    strict: bool = False,
) -> bool:
    """Try to acquire an exclusive flock with bounded retry + exponential backoff.

    H3 + H4 fix: replaces the raw ``fcntl.flock(..., LOCK_EX)`` call in
    memory_mcp.py. By default uses LOCK_NB so the call never blocks
    longer than ``max_attempts * sum(backoff)`` seconds, even if another
    process is holding the lock.

    Args:
        lock_file: An open file object whose ``fileno()`` is flockable.
        max_attempts: Maximum number of flock attempts (default 5).
        initial_backoff: Seconds to wait before the second attempt
            (default 0.05 = 50 ms).
        backoff_multiplier: Each subsequent retry waits this much longer
            than the previous one (default 2.0 = exponential).
        max_backoff: Cap on the per-attempt sleep (default 1.0 s).
        nonblocking: If True (default), use LOCK_NB; if False, the
            kernel may block indefinitely on contention.
        strict: If True, raise :class:`FileLockError` after retries
            are exhausted instead of returning False. Critical write
            callers (save_memory, the MEMORY.md index updater) must
            set this so a held lock surfaces as a real error rather
            than silently dropping the lock and racing on the write.

    Returns:
        True if the lock was acquired, False otherwise. On non-fcntl
        platforms the function returns False on the first attempt
        (caller should treat as best-effort and continue) regardless
        of ``strict``.

    Raises:
        FileLockError: when ``strict=True`` and all attempts fail
            (or fcntl is unavailable on the platform).
    """
    backoff = initial_backoff
    for attempt in range(1, max_attempts + 1):
        if _try_flock(lock_file, nonblocking=nonblocking):
            return True
        if attempt == max_attempts:
            break
        time.sleep(min(backoff, max_backoff))
        backoff *= backoff_multiplier
    logger.warning(
        "acquire_flock_with_retry: gave up after %d attempts on %s",
        max_attempts,
        getattr(lock_file, "name", "<unknown>"),
    )
    if strict:
        raise FileLockError(
            f"acquire_flock_with_retry(strict=True): could not acquire "
            f"flock on {getattr(lock_file, 'name', '<unknown>')} after "
            f"{max_attempts} attempts"
        )
    return False


def release_flock(lock_file) -> bool:
    """Best-effort lock release + file close using pluggable LockManager.

    Safe to call on a lock that was never acquired (it just no-ops).
    """
    if lock_file is None:
        return False
    token = getattr(lock_file, "_lease_token", None)
    if token:
        import os
        from infra.lock_manager import get_lock_manager
        lm = get_lock_manager()
        key = os.path.abspath(lock_file.name)
        try:
            lm.release_lock(key, token)
        except Exception as e:
            logger.warning("release_flock lock manager release failed: %s", e)
        setattr(lock_file, "_lease_token", None)
    try:
        lock_file.close()
    except Exception as e:
        logger.warning("release_flock file close failed: %s", e)
    return True
