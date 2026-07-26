"""Per-agent singleton guard for the MCP server process.

Ensures only one memory_mcp instance runs per agent per memory directory.
The lock file is scoped by MEMORY_AGENT_ID so multiple agents (e.g.,
OPENCODE and MIMOCODE) can each run their own MCP server against the
same memory directory without conflict.

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
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_agent_id = ""
_MCP_LOCK_FILENAME = ".mcp_server.lock"

_MAX_STALE_RETRIES = 4
_STALE_RETRY_INITIAL_BACKOFF = 0.5

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


def _check_pid_alive(pid: int) -> bool:
    """Check if a PID is alive AND not a zombie.

    Uses os.kill(pid, 0) for the standard liveness check, PLUS
    verifies the process is not a zombie (Z state). Zombie processes
    pass os.kill(pid, 0) because their PID is still in the table,
    but they are dead and their lock is stale.
    """
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return False
    try:
        import subprocess
        result = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
        state = result.stdout.strip()
        if state == "Z" or state == "Z+":
            return False
    except Exception:
        pass
    return True


def _try_override_stale_lock() -> bool:
    """Try to acquire the lock when the previous owner's PID is dead.

    Uses a blocking acquire with bounded retry (up to 5 attempts) to avoid
    racing another startup that also detected the stale PID at the same time.
    """
    global _lock_fd, _lock_path

    if _lock_path is None:
        return False

    try:
        from infra.file_lock import acquire_flock_with_retry

        _lock_fd = open(_lock_path, "w")
        got = acquire_flock_with_retry(
            _lock_fd,
            max_attempts=5,
            nonblocking=False,
            initial_backoff=0.2,
            strict=True,
        )
        if got:
            try:
                _lock_fd.write(str(os.getpid()))
                _lock_fd.flush()
            except OSError:
                pass
            atexit.register(release_mcp_singleton)
            logger.info("mcp_singleton: acquired (stale override, pid=%d)", os.getpid())
            return True
        else:
            try:
                _lock_fd.close()
            except OSError:
                pass
            _lock_fd = None
            return False
    except Exception as exc:
        logger.warning("mcp_singleton: stale override failed: %s", exc)
        if _lock_fd is not None:
            try:
                _lock_fd.close()
            except OSError:
                pass
        _lock_fd = None
        return False


def acquire_mcp_singleton() -> bool:
    """Try to acquire the MCP server singleton lock.

    Returns True if this process is the sole MCP server (lock acquired).
    Returns False if another MCP server is already running (lock held).
    """
    global _lock_fd, _lock_path, _agent_id, _MCP_LOCK_FILENAME

    _agent_id = os.environ.get("MEMORY_AGENT_ID", "").strip().lower()
    _MCP_LOCK_FILENAME = f".mcp_server.lock.{_agent_id}" if _agent_id else ".mcp_server.lock"

    memory_dir = _get_memory_dir()
    _lock_path = memory_dir / _MCP_LOCK_FILENAME

    _lock_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        _lock_fd = open(_lock_path, "w")
    except OSError as exc:
        logger.warning("mcp_singleton: cannot open lock file %s: %s", _lock_path, exc)
        return True

    try:
        from infra.file_lock import acquire_flock_with_retry

        got = acquire_flock_with_retry(
            _lock_fd,
            max_attempts=3,
            nonblocking=True,
            initial_backoff=0.3,
            strict=True,
        )
        if got:
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
    except Exception as exc:
        logger.warning("mcp_singleton: flock failed: %s", exc)

    if _lock_fd is not None:
        try:
            _lock_fd.close()
        except OSError:
            pass
    _lock_fd = None

    # Retry loop for stale-lock override: when two instances start
    # simultaneously and both detect a dead PID, the loser should not
    # exit immediately — it retries with backoff, re-checking PID
    # liveness each time in case the winner crashes during init.
    for retry in range(1, _MAX_STALE_RETRIES + 1):
        if _lock_path is None or not _lock_path.exists():
            logger.info("mcp_singleton: lock file gone, overriding stale lock")
            return _try_override_stale_lock()

        try:
            existing_pid_str = _lock_path.read_text().strip()
        except OSError as pid_exc:
            logger.info("mcp_singleton: cannot read lock file (%s) — overriding", pid_exc)
            return _try_override_stale_lock()

        if not existing_pid_str:
            # Empty lock file — previous process died before writing PID
            logger.info("mcp_singleton: empty lock file — overriding stale lock (attempt %d)", retry)
            return _try_override_stale_lock()

        try:
            existing_pid = int(existing_pid_str)
        except ValueError:
            logger.info("mcp_singleton: PID parse failed — overriding stale lock (attempt %d)", retry)
            return _try_override_stale_lock()

        if not _check_pid_alive(existing_pid):
            logger.info(
                "mcp_singleton: PID %s is dead — overriding stale lock (attempt %d)",
                existing_pid,
                retry,
            )
            return _try_override_stale_lock()

        # PID is alive — another instance is running. If this is not our
        # last retry, wait with exponential backoff and re-check.
        if retry < _MAX_STALE_RETRIES:
            backoff = min(_STALE_RETRY_INITIAL_BACKOFF * (2 ** (retry - 1)), 5.0)
            logger.info(
                "mcp_singleton: PID %s is alive, retrying stale override in %.1fs (attempt %d/%d)",
                existing_pid,
                backoff,
                retry,
                _MAX_STALE_RETRIES,
            )
            time.sleep(backoff)
        else:
            logger.info(
                "mcp_singleton: PID %s is alive after %d attempts — giving up",
                existing_pid,
                _MAX_STALE_RETRIES,
            )

    return False


def release_mcp_singleton() -> None:
    """Release the MCP server singleton lock.

    Safe to call multiple times; no-op if already released.
    Uses ``release_flock`` to properly clean up both the OS-level flock
    AND the DB-level lock entry in ``system_locks`` (prevents stale 300s
    TTL entries that block restarts).
    """
    global _lock_fd, _lock_path

    if _lock_fd is None:
        return

    try:
        from infra.file_lock import release_flock
        release_flock(_lock_fd)
    except Exception:
        pass

    _lock_fd = None

    if _lock_path is not None:
        try:
            _lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    logger.info("mcp_singleton: released lock")
