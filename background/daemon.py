#!/usr/bin/env python3
"""Daemon process for auto-save inbox processing.

Extracted from auto_save.py in Phase 3.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from background.circuit_breaker import (
    _DAEMON_LOCKS,
    _DAEMON_STOP_REQUESTED,
    _check_circuit_timeout_expiry,
    _update_shared_memory_state,
)
from background.inbox import (
    get_auto_save_inbox_path,
    get_auto_save_lock_path,
    get_auto_save_pid_path,
    _drain_inbox,
    _process_inbox_batch,
    _register_in_daemon_manifest,
    _unregister_from_daemon_manifest,
    _write_pid_file,
)

logger = logging.getLogger("auto_save.daemon")

_DEFAULT_BATCH_INTERVAL_S = 0.5
_DEFAULT_BATCH_SIZE = 50
_DEFAULT_DAEMON_IDLE_S = 300


def _batch_interval_s() -> float:
    return _DEFAULT_BATCH_INTERVAL_S


def _batch_size() -> int:
    return _DEFAULT_BATCH_SIZE


def _daemon_idle_s() -> float:
    return _DEFAULT_DAEMON_IDLE_S


def _log_structured(level: str, event: str, **fields: object) -> None:
    """Emit a structured JSON log entry for observability."""
    import json as _json

    log_entry = {"event": event, **fields}
    getattr(logger, level)(_json.dumps(log_entry))


def _wait_for_file_modification(file_path: Path, timeout: float) -> None:
    """Wait for file or directory changes using kqueue or inotify, fallback to sleep."""
    if timeout <= 0:
        return

    # 1. Try kqueue (macOS / BSD)
    try:
        import select as _select

        if hasattr(_select, "kqueue"):
            kqueue_fn = getattr(_select, "kqueue")
            kevent_fn = getattr(_select, "kevent")
            KQ_FILTER_VNODE = getattr(_select, "KQ_FILTER_VNODE")
            KQ_EV_ADD = getattr(_select, "KQ_EV_ADD")
            KQ_EV_CLEAR = getattr(_select, "KQ_EV_CLEAR")
            KQ_NOTE_WRITE = getattr(_select, "KQ_NOTE_WRITE")
            KQ_NOTE_EXTEND = getattr(_select, "KQ_NOTE_EXTEND")

            kq = kqueue_fn()
            dir_fd = os.open(str(file_path.parent), os.O_RDONLY)
            kevents = [
                kevent_fn(
                    dir_fd,
                    filter=KQ_FILTER_VNODE,
                    flags=KQ_EV_ADD | KQ_EV_CLEAR,
                    fflags=KQ_NOTE_WRITE,
                )
            ]
            file_fd = None
            if file_path.exists():
                try:
                    file_fd = os.open(str(file_path), os.O_RDONLY)
                    kevents.append(
                        kevent_fn(
                            file_fd,
                            filter=KQ_FILTER_VNODE,
                            flags=KQ_EV_ADD | KQ_EV_CLEAR,
                            fflags=KQ_NOTE_WRITE | KQ_NOTE_EXTEND,
                        )
                    )
                except OSError as exc:
                    logger.debug("auto-save daemon: kqueue fd open failed: %s", exc)
            try:
                kq.control(kevents, len(kevents), timeout)
            finally:
                os.close(dir_fd)
                if file_fd is not None:
                    os.close(file_fd)
                kq.close()
            return
    except Exception as e:
        logger.debug("auto-save daemon: kqueue watch failed: %s", e)

    # 2. Try inotify (Linux)
    try:
        assert _inotify_init is not None
        fd = _inotify_init()
        # masks: IN_CREATE=0x100, IN_DELETE=0x200, IN_MOVED_TO=0x80, IN_MODIFY=0x2
        mask_dir = 0x100 | 0x200 | 0x80
        assert _inotify_add_watch is not None
        _inotify_add_watch(fd, str(file_path.parent), mask_dir)
        if file_path.exists():
            try:
                _inotify_add_watch(fd, str(file_path), 0x2)
            except OSError as exc:
                logger.debug(
                    "auto-save daemon: inotify add-watch (file) failed: %s", exc
                )
        try:
            import select as _select

            _select.select([fd], [], [], timeout)
        finally:
            os.close(fd)
        return
    except Exception as e:
        logger.debug("auto-save daemon: inotify watch failed: %s", e)

    # 3. Fallback to sleep
    time.sleep(min(timeout, 0.05))


# Inotify handles (loaded lazily to avoid Linux-only import error on macOS)
try:
    from inotify_simple import inotify_init, inotify_add_watch

    _inotify_init = inotify_init
    _inotify_add_watch = inotify_add_watch
except ImportError:
    _inotify_init = None  # type: ignore[assignment]
    _inotify_add_watch = None  # type: ignore[assignment]



def _cleanup_stale_processing_files(max_age_s: float = 3600.0) -> None:
    """Delete orphan .processing.{pid} files older than max_age_s.

    SIGKILL between inbox rename and unlink can leave stale processing
    files around.  The daemon is idempotent (ON CONFLICT on upsert)
    so re-processing is safe, but the file clutter is confusing and
    can mask real inbox issues.  Clean up on startup so we start
    fresh.
    """
    import time as _time
    try:
        inbox = get_auto_save_inbox_path()
        now = _time.time()
        for p in inbox.parent.glob(f"{inbox.name}.processing.*"):
            try:
                age = now - p.stat().st_mtime
                if age > max_age_s:
                    p.unlink(missing_ok=True)
                    logger.info("auto-save daemon: removed stale %s (age=%.0fs)", p.name, age)
            except Exception as _ce:
                logger.debug("auto-save daemon: could not clean %s: %s", p.name, _ce)
    except Exception as _e:
        logger.debug("auto-save daemon: cleanup_stale_processing_files failed: %s", _e)


def run_daemon(stop_event: Optional["threading.Event"] = None) -> None:  # noqa: F821
    """Long-running daemon: tail the inbox, process in batches.

    Loop body:

    1. Drain the inbox into an in-memory buffer.
    2. When the buffer reaches ``AUTO_SAVE_BATCH_SIZE`` or
       ``AUTO_SAVE_BATCH_INTERVAL`` seconds have passed since the
       first unprocessed entry, flush the buffer.
    3. Otherwise, sleep 50ms and check again.
    4. Exit on SIGTERM/SIGINT, on ``stop_event`` being set, or after
       ``AUTO_SAVE_DAEMON_IDLE_S`` seconds of inbox silence.

    Acquired the flock at startup so two daemons never run
    concurrently for the same memory dir.  Writes the PID file for
    the opencode hook's liveness check.
    """
    # Install signal handlers BEFORE the flock acquisition.  Without
    # this, a daemon that fails to acquire the flock (because another
    # daemon already holds it) has no signal handler installed and
    # SIGTERM is ignored.  We observed three such "ghost" daemons
    # (PIDs 21117, 21439, 21886) that survived SIGTERM during the
    # 2026-06-22 system audit and required SIGKILL to terminate.
    # The fix: install handlers first, then check the lock.  If the
    # SIGTERM arrives between the handler install and the lock
    # check, the next check of ``_DAEMON_STOP_REQUESTED`` will see it
    # and the daemon will exit cleanly.
    global _DAEMON_STOP_REQUESTED
    import signal as _signal

    def _on_signal(signum, frame):
        global _DAEMON_STOP_REQUESTED
        _DAEMON_STOP_REQUESTED = True
        if stop_event is not None:
            stop_event.set()
        logger.info("auto-save daemon: received signal %d", signum)

    _signal.signal(_signal.SIGTERM, _on_signal)
    _signal.signal(_signal.SIGINT, _on_signal)

    # Acquire the daemon flock so we are the only daemon for this
    # memory dir.  If the flock is held, exit silently — another
    # daemon is already running.  Uses the same fd-keeps-alive pattern
    # as cron/_flock.py: a module-level dict holds the FD so the GC
    # doesn't reap it and release the lock mid-run.
    from infra.file_lock import acquire_flock_with_retry, release_flock

    lock_path = get_auto_save_lock_path()
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = open(lock_path, "w", encoding="utf-8")
    except OSError as e:
        logger.warning("auto-save daemon: cannot open lock file: %s", e)
        return
    if not acquire_flock_with_retry(lock_fd, max_attempts=1, nonblocking=True):
        try:
            lock_fd.close()
        except Exception:
            pass
        logger.info("auto-save daemon: another instance holds the lock; exiting")
        return
    # Pin the FD in a module-level dict so it survives the daemon's
    # lifetime (otherwise Python's GC would close it and the flock
    # would release, letting a second daemon start).
    _DAEMON_LOCKS["auto_save_daemon"] = lock_fd
    try:
        _cleanup_stale_processing_files()
        _write_pid_file()
        _register_in_daemon_manifest()
        _log_structured("info", "auto_save_daemon_started", pid=os.getpid())

        buffer: list[dict] = []
        last_flush = time.time()
        last_activity = time.time()
        last_sm_refresh = time.time()  # S1: shared memory refresh
        interval = _batch_interval_s()
        size_cap = _batch_size()
        idle_limit = _daemon_idle_s()

        while True:
            # 1. Stop requested?
            if _DAEMON_STOP_REQUESTED:
                break
            if stop_event is not None and stop_event.is_set():
                break

            # 1b. Check for circuit timeout expiry (P0-11 fix).
            _check_circuit_timeout_expiry()

            # 1c. S1 (2026-06-23): refresh shared memory every 5
            # seconds so CLI hooks see current state even when no
            # state change has happened. The shared memory write
            # is sub-millisecond; this is cheap.
            now = time.time()
            if now - last_sm_refresh > 5.0:
                _update_shared_memory_state()
                last_sm_refresh = now

            # 2. Idle exit (even when buffer is empty)?
            # Without this check a daemon with an empty buffer never exits,
            # creating zombie processes that accumulate across sessions.
            # (2026-06-25 fix: removed the `if buffer` guard)
            if (time.time() - last_activity) > idle_limit:
                _log_structured(
                    "info",
                    "auto_save_daemon_idle_exit",
                    idle_seconds=int(idle_limit),
                    buffer_size=len(buffer),
                )
                break

            # 3. Drain any new entries from the inbox.
            try:
                entries = _drain_inbox()
            except Exception as exc:
                logger.warning("auto-save daemon: drain failed: %s", exc)
                entries = []

            if entries:
                buffer.extend(entries)
                last_activity = time.time()

            # 4. Flush batch when big enough or interval elapsed.
            if buffer and (
                len(buffer) >= size_cap or (time.time() - last_flush) >= interval
            ):
                try:
                    summary = _process_inbox_batch(buffer)
                    _log_structured(
                        "info",
                        "auto_save_batch_flushed",
                        saved=summary.get("saved", 0),
                        skipped=summary.get("skipped", 0),
                        failed=summary.get("failed", 0),
                        buffer_size=0,
                    )
                except Exception as exc:
                    logger.warning(
                        "auto-save daemon: batch flush failed: %s", exc
                    )
                    _log_structured(
                        "warning", "auto_save_batch_flush_failed", error=str(exc)
                    )
                buffer = []
                last_flush = time.time()
                last_activity = time.time()

            # 5. Wait for new file activity before the next iteration.
            _wait_for_file_modification(
                get_auto_save_inbox_path(), _batch_interval_s()
            )

    except Exception as exc:
        logger.exception("auto-save daemon: fatal error: %s", exc)
    finally:
        # Final flush — process anything still in the buffer.
        try:
            if buffer:
                _process_inbox_batch(buffer)
                _log_structured(
                    "info",
                    "auto_save_final_flush",
                    buffer_size=len(buffer),
                )
        except Exception as exc:
            logger.warning("auto-save daemon: final flush failed: %s", exc)
            _log_structured(
                "warning", "auto_save_final_flush_failed", error=str(exc)
            )

        # Always clean up PID and lock on exit.
        try:
            pid_path = get_auto_save_pid_path()
            pid_path.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            lock_path = get_auto_save_lock_path()
            fd = _DAEMON_LOCKS.pop("auto_save_daemon", None)
            if fd is not None:
                release_flock(fd)
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            _unregister_from_daemon_manifest()
        except Exception:
            pass
        _log_structured("info", "auto_save_daemon_stopped", pid=os.getpid())
