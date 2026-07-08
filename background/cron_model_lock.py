"""File-based mutex for crons that load ML models.

Multiple crons may be scheduled at the same time (e.g. cron_kg_extraction
and cron_vec_index_rebuild both load embedding models into GPU memory).
Without coordination, concurrent model loads can exhaust RAM / VRAM.

This module provides a ``cron_model_lock`` context manager backed by
``fcntl.flock`` on a file in ``<memory_dir>/.cron_model_lock/<name>.lock``.
Only one process can hold the lock at a time; other processes skip with a
warning instead of corrupting memory.

Usage::

    from background.cron_model_lock import cron_model_lock

    with cron_model_lock("kg_extraction", timeout=600.0):
        load_model()  # expensive, mutex-protected
        run_extraction()
"""

from __future__ import annotations

import logging

import contextlib
import fcntl
import os
import time
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

# Default lock directory: `<memory_dir>/.cron_model_lock/`
# Operator can override via MEMORY_CRON_LOCK_DIR env var.
_DEFAULT_LOCK_DIR_NAME = ".cron_model_lock"

# Max seconds a cron is allowed to hold the model lock.
MAX_CRON_RUNTIME_S: float = 600.0

# Stale lock threshold: locks older than this are considered orphaned.
_STALE_THRESHOLD_S: float = 3600.0  # 1 hour


def _get_lock_dir() -> Path:
    """Return the directory used for cron model lock files."""
    env_dir = os.environ.get("MEMORY_CRON_LOCK_DIR")
    if env_dir:
        return Path(env_dir)
    try:
        from config import get_config
        cfg = get_config()
        db_path = Path(cfg.db_path)
        lock_dir = db_path.parent / _DEFAULT_LOCK_DIR_NAME
    except Exception as e:
        logger.warning("_get_lock_dir failed: %s", e)
        lock_dir = Path(".cron_model_lock")
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir


def _is_stale(lock_path: Path) -> bool:
    """Return True if the lock file is older than the stale threshold."""
    try:
        age = time.time() - lock_path.stat().st_mtime
        return age > _STALE_THRESHOLD_S
    except OSError:
        return False


@contextlib.contextmanager
def cron_model_lock(
    name: str,
    timeout: float = MAX_CRON_RUNTIME_S,
    *,
    _lock_dir: Path | None = None,
) -> Generator[None, None, None]:
    """Acquire an exclusive model-load mutex for cron ``name``.

    Args:
        name: Unique cron identifier (e.g. ``"kg_extraction"``).
        timeout: Max seconds to wait for the lock. ``0`` means non-blocking.

    Yields:
        None

    Raises:
        TimeoutError: If the lock could not be acquired within ``timeout``.
        OSError: On filesystem errors.
    """
    lock_dir = _lock_dir or _get_lock_dir()
    lock_path = lock_dir / f"{name}.lock"
    fd = None
    acquired = False
    try:
        if timeout > 0:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if fd is not None:
                        os.close(fd)
                        fd = None
                    if time.monotonic() > deadline:
                        raise TimeoutError(
                            f"cron_model_lock({name}): timed out after {timeout}s"
                        )
                    time.sleep(0.5)
        else:
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        if acquired:
            logger.info("cron_model_lock(%s): acquired", name)
            yield
    finally:
        if fd is not None:
            try:
                if acquired:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            except OSError:
                pass
        if acquired:
            logger.info("cron_model_lock(%s): released", name)


def release_cron_model_lock(name: str) -> None:
    """Force-release the lock held by *name* (for stale-lock recovery)."""
    lock_dir = _get_lock_dir()
    lock_path = lock_dir / f"{name}.lock"
    try:
        if lock_path.exists():
            lock_path.unlink()
            logger.info("cron_model_lock(%s): force-released", name)
    except OSError as exc:
        logger.warning("cron_model_lock(%s): release failed: %s", name, exc)


def cleanup_stale_locks(*, _lock_dir: Path | None = None) -> list[str]:
    """Remove lock files older than ``_STALE_THRESHOLD_S``.

    Args:
        _lock_dir: Override lock directory (test only). Uses ``_get_lock_dir()``
            when not provided.

    Returns list of cleaned lock names.
    """
    cleaned: list[str] = []
    lock_dir = _lock_dir or _get_lock_dir()
    for lock_path in lock_dir.glob("*.lock"):
        if _is_stale(lock_path):
            name = lock_path.stem
            try:
                lock_path.unlink()
                cleaned.append(name)
                logger.info("cron_model_lock(%s): cleaned stale lock", name)
            except OSError as exc:
                logger.warning("cron_model_lock(%s): cleanup failed: %s", name, exc)
    return cleaned
