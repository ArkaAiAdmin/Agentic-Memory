"""Flock helper for cron scripts.

Wraps ``acquire_flock_with_retry`` from ``file_lock.py`` so each cron
script can serialize itself against a previous overlapping run with a
single function call.

H-fix (2026-06-22): replaces the no-lock behaviour of all 23 cron
scripts. Each cron gets its own lock file at
``<GLOBAL_MEM_DIR>/locks/<name>.lock`` so:

  * Two instances of the SAME cron never run concurrently (flock -n
    semantics).
  * Two DIFFERENT crons can still overlap (they have different lock
    files) — the SQLite WAL handles write ordering at the DB level.
  * If the system crashes mid-run, the next cron tick can still
    acquire the lock because ``fcntl`` locks are released when the
    process dies.

If the lock cannot be acquired within ``max_attempts`` (default 5)
the helper exits 0 (best-effort: the other instance is doing the
work, so we skip). The script's caller (cron) sees a clean
no-op log line.

Usage:

    from _flock import acquire_lock_or_exit

    def main():
        acquire_lock_or_exit("cron_consolidate")
        # ... rest of main ...
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# Make ``memory_config`` importable even if the calling cron script
# hasn't yet run its own ``sys.path.insert(...)`` bootstrap (the
# ``from _flock import`` call is added by the flock patcher BEFORE
# the cron script's own path setup runs). This means ``_flock.py``
# can be imported at the top of any cron script.
_THIS = Path(__file__).resolve().parent
if _THIS.name == "cron":
    _REPO_ROOT = _THIS.parent
else:
    _REPO_ROOT = _THIS
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_config import GLOBAL_MEM_DIR

_LOCK_DIR = GLOBAL_MEM_DIR / "locks"

# Module-level keep-alive for the open lock file descriptors.
# ``fcntl.flock`` releases when the FD is closed, and a local
# variable in ``acquire_lock_or_exit`` would go out of scope
# immediately after the function returns — letting the next cron
# tick acquire the same lock. Hold the FDs in this dict so they
# stay open for the lifetime of the process.
_OPEN_LOCKS: dict[str, Any] = {}

# Re-export the underlying primitives so cron scripts that want more
# control (e.g. ``strict=True``) can use them directly without adding
# a second import line.
try:
    from file_lock import (  # type: ignore[import-not-found]
        FileLockError,
        acquire_flock_with_retry,
        release_flock,
    )
except ImportError:  # pragma: no cover
    FileLockError = None  # type: ignore[assignment,misc]

    def acquire_flock_with_retry(*_args: Any, **_kwargs: Any) -> bool:
        return False

    def release_flock(*_args: Any, **_kwargs: Any) -> bool:
        return False


def _lock_path(name: str) -> Path:
    """Resolve the per-cron lock file. ``name`` should be the bare
    cron name like ``"cron_consolidate"`` (no extension, no path).
    """
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name)
    return _LOCK_DIR / f"{safe}.lock"


def acquire_lock_or_exit(name: str, max_attempts: int = 5) -> None:
    """Acquire the per-cron flock, or exit 0 if it's already held.

    Intended to be the first call inside ``main()`` of any cron
    script. On success, returns ``None``; the caller proceeds
    normally. On failure (another instance is running), prints a
    single log line and ``sys.exit(0)`` so cron doesn't see a
    non-zero exit and the run is treated as a clean no-op.

    The lock FD is stored in the module-level ``_OPEN_LOCKS`` dict
    so it survives the function return — otherwise Python would
    close the FD and ``fcntl.flock`` would release the lock
    immediately. The lock is released automatically when the
    process exits (flock semantics), so an unhandled exception in
    ``main()`` will not leave a stale lock behind.
    """
    if name in _OPEN_LOCKS:
        # Already locked by this process; no-op.
        return
    _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(name)
    try:
        lock_fd = open(lock_path, "w", encoding="utf-8")
    except OSError as exc:
        # If we can't even open the lock file, fall through and run
        # the script anyway — flock is best-effort, not a hard gate.
        print(
            f"[{name}] WARN: could not open lock file {lock_path}: {exc}",
            file=sys.stderr,
        )
        return
    if not acquire_flock_with_retry(
        lock_fd, max_attempts=max_attempts, nonblocking=True
    ):
        try:
            lock_fd.close()
        except Exception:
            pass
        # Another instance is running; skip this tick.
        print(f"[{name}] skipped: another instance holds {lock_path}")
        sys.exit(0)
    # Pin the FD in a module-level dict so the GC doesn't reap it.
    _OPEN_LOCKS[name] = lock_fd


__all__ = [
    "acquire_lock_or_exit",
    "acquire_flock_with_retry",
    "release_flock",
    "FileLockError",
]
