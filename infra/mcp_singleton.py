"""Singleton guard for the MCP server process.

Ensures only one memory_mcp instance runs per memory directory.
Uses flock-based mutual exclusion (same pattern as the auto-save daemon
in inbox.py).

Usage:
    from infra.mcp_singleton import acquire_mcp_singleton, release_mcp_singleton

    if not acquire_mcp_singleton():
        logger.error("Another MCP server is already running")
        sys.exit(1)
    try:
        mcp.run()
    finally:
        release_mcp_singleton()
"""

from __future__ import annotations

import atexit
import io
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_MCP_LOCK_FILENAME = ".mcp_server.lock"

# Module-level state for cleanup.
_lock_fd: Optional[io.TextIOWrapper] = None
_lock_path: Optional[Path] = None


def _get_memory_dir() -> Path:
    """Resolve the active memory directory."""
    env = os.environ.get("MEMORY_DB_PATH")
    if env:
        return Path(env).parent
    try:
        from infra.infrastructure import resolve_active_memory_dir
        return resolve_active_memory_dir()
    except Exception:
        return Path.home() / ".config" / "agentic-memory" / "memory"


def acquire_mcp_singleton() -> bool:
    """Try to acquire the MCP server singleton lock.

    Returns True if this process is the sole MCP server (lock acquired).
    Returns False if another MCP server is already running (lock held).
    """
    global _lock_fd, _lock_path

    memory_dir = _get_memory_dir()
    _lock_path = memory_dir / _MCP_LOCK_FILENAME

    # Ensure the directory exists.
    _lock_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        _lock_fd = open(_lock_path, "w")
    except OSError as exc:
        logger.warning("mcp_singleton: cannot open lock file %s: %s", _lock_path, exc)
        # Fail open — don't block startup if the lock file is inaccessible.
        return True

    try:
        from infra.file_lock import acquire_flock_with_retry

        got = acquire_flock_with_retry(
            _lock_fd,
            max_attempts=1,
            nonblocking=True,
            strict=True,
        )
        if got:
            # Write PID for diagnostics.
            try:
                _lock_fd.write(str(os.getpid()))
                _lock_fd.flush()
            except OSError:
                pass
            atexit.register(release_mcp_singleton)
            logger.info(
                "mcp_singleton: acquired lock (pid=%d, path=%s)",
                os.getpid(),
                _lock_path,
            )
            return True
        else:
            # Lock held by another process.
            try:
                _lock_fd.close()
            except OSError:
                pass
            _lock_fd = None
            return False
    except Exception as exc:
        logger.warning("mcp_singleton: flock failed: %s", exc)
        if _lock_fd is not None:
            try:
                _lock_fd.close()
            except OSError:
                pass
        _lock_fd = None
        # Fail open on unexpected errors.
        return True


def release_mcp_singleton() -> None:
    """Release the MCP server singleton lock.

    Safe to call multiple times; no-op if already released.
    """
    global _lock_fd, _lock_path

    if _lock_fd is None:
        return

    try:
        import fcntl as _fcntl
        _fcntl.flock(_lock_fd.fileno(), _fcntl.LOCK_UN)
    except Exception:
        pass

    try:
        _lock_fd.close()
    except OSError:
        pass

    _lock_fd = None

    # Remove the lock file so stale locks don't confuse future startups.
    if _lock_path is not None:
        try:
            _lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    logger.info("mcp_singleton: released lock")
