"""File-locking helpers (flock with bounded retry).

Extracted from memory_common.py during the 6-module refactor.

Provides:
  * ``acquire_flock_with_retry``: flock with exponential backoff.
  * ``release_flock``: best-effort unlock + close.
  * ``_try_flock`` (private): single flock attempt.
"""

from __future__ import annotations

import ctypes
import errno
import logging
logger = logging.getLogger(__name__)

import os
import subprocess
import sys
import time

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None  # type: ignore[assignment]


__all__ = [
    "acquire_flock_with_retry",
    "release_flock",
    "FileLockError",
    "lock_fd",
    "unlock_fd",
]

_LOCKFILE_FAIL_IMMEDIATELY = 0x1
_LOCKFILE_EXCLUSIVE_LOCK = 0x2
_ERROR_LOCK_VIOLATION = 33
_ERROR_NOT_LOCKED = 158


class _OverlappedOffset(ctypes.Structure):
    _fields_ = [
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
    ]


class _OverlappedUnion(ctypes.Union):
    _fields_ = [
        ("_offset", _OverlappedOffset),
        ("Pointer", ctypes.c_void_p),
    ]


class _Overlapped(ctypes.Structure):
    _anonymous_ = ("_u",)
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("_u", _OverlappedUnion),
        ("hEvent", ctypes.c_void_p),
    ]


_KERNEL32 = None


def _get_kernel32():
    global _KERNEL32
    if _KERNEL32 is None:
        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            raise OSError("WinDLL is unavailable on this platform")
        k32 = win_dll("kernel32", use_last_error=True)
        k32.LockFileEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(_Overlapped),
        ]
        k32.LockFileEx.restype = ctypes.c_int
        k32.UnlockFileEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(_Overlapped),
        ]
        k32.UnlockFileEx.restype = ctypes.c_int
        _KERNEL32 = k32
    return _KERNEL32


def _raise_win32_error(err: int) -> None:
    win_error = getattr(ctypes, "WinError", None)
    if win_error is not None:
        raise win_error(err)
    raise OSError(err, f"Win32 error {err}")


def lock_fd(fd: int, nonblocking: bool = True) -> bool:
    """Acquire an exclusive advisory/byte-range lock on file descriptor ``fd``.

    On POSIX, delegates to ``fcntl.flock`` with ``LOCK_EX`` (plus ``LOCK_NB``
    when ``nonblocking=True``). On Windows, delegates to ``kernel32.LockFileEx``
    on 1 byte at offset 0.

    Returns ``True`` when the lock is acquired, ``False`` on lock contention
    when ``nonblocking=True``, and raises ``OSError`` on unexpected failures.
    """
    if _fcntl is not None:
        flags = _fcntl.LOCK_EX | (_fcntl.LOCK_NB if nonblocking else 0)
        try:
            _fcntl.flock(fd, flags)
            return True
        except BlockingIOError:
            return False
        except OSError as exc:
            eagain = getattr(errno, "EAGAIN", None)
            ewouldblock = getattr(errno, "EWOULDBLOCK", None)
            eacces = getattr(errno, "EACCES", None)
            if exc.errno in (eagain, ewouldblock, eacces):
                return False
            raise

    if sys.platform == "win32":
        import msvcrt

        handle = msvcrt.get_osfhandle(fd)
        k32 = _get_kernel32()
        flags = _LOCKFILE_EXCLUSIVE_LOCK | (
            _LOCKFILE_FAIL_IMMEDIATELY if nonblocking else 0
        )
        overlapped = _Overlapped()
        ok = k32.LockFileEx(
            ctypes.c_void_p(handle),
            flags,
            0,
            1,
            0,
            ctypes.byref(overlapped),
        )
        if ok:
            return True
        err = ctypes.get_last_error()
        if nonblocking and err == _ERROR_LOCK_VIOLATION:
            return False
        _raise_win32_error(err)

    raise OSError("No file locking implementation available on this platform")


def unlock_fd(fd: int) -> None:
    """Release an exclusive lock on file descriptor ``fd``."""
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
        return

    if sys.platform == "win32":
        import msvcrt

        handle = msvcrt.get_osfhandle(fd)
        k32 = _get_kernel32()
        overlapped = _Overlapped()
        ok = k32.UnlockFileEx(
            ctypes.c_void_p(handle),
            0,
            1,
            0,
            ctypes.byref(overlapped),
        )
        if not ok:
            err = ctypes.get_last_error()
            if err != _ERROR_NOT_LOCKED:
                _raise_win32_error(err)
        return

    raise OSError("No file locking implementation available on this platform")


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
    """Single flock attempt on the provided file descriptor.

    Uses the lock_file's actual fileno() so concurrent holders of the same
    file (including raw flock callers in tests) are detected.
    """
    if lock_file is None or not hasattr(lock_file, "fileno"):
        return False
    try:
        return lock_fd(lock_file.fileno(), nonblocking=nonblocking)
    except (IOError, OSError, ValueError):
        return False


def _is_stale_lock(lock_path) -> bool:
    """Return True if the lock file exists on disk but no live process holds a lock on it.

    On Windows, probes the lock file directly via ``lock_fd(..., nonblocking=True)``
    without invoking POSIX tools. On POSIX, uses ``fuser`` and falls back to
    ``lsof``. Neither command finding an FD means either the OS released the
    flock (process died) or the file is simply not open — both are stale.
    """
    if not lock_path.exists():
        return False
    if sys.platform == "win32":
        fd = None
        try:
            fd = os.open(str(lock_path), os.O_RDWR)
            if lock_fd(fd, nonblocking=True):
                try:
                    unlock_fd(fd)
                except OSError:
                    pass
                return True
            return False
        except FileNotFoundError:
            return False
        except OSError:
            return False
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
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
    """Best-effort lock release + file close on the file descriptor.

    Safe to call on a lock that was never acquired (it just no-ops).
    """
    if lock_file is None:
        return False
    try:
        unlock_fd(lock_file.fileno())
    except (IOError, OSError, ValueError) as e:
        logger.debug("release_flock unlock failed: %s", e)
    try:
        lock_file.close()
    except Exception as e:
        logger.warning("release_flock file close failed: %s", e)
    return True
