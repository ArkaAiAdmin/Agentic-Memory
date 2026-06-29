#!/usr/bin/env python3
"""Daemon process for auto-save inbox processing.

Extracted from auto_save.py in Phase 3.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


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
    from file_lock import acquire_flock_with_retry, release_flock

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

            # 3. Drain inbox.
            entries = _drain_inbox()
            if entries:
                buffer.extend(entries)
                last_activity = time.time()
            elif buffer and (time.time() - last_activity) > idle_limit:
                # No new entries for a long time — flush what we have
                # and exit so the next call respawns us.
                break

            # 4. Flush if size or interval reached.
            now = time.time()
            should_flush = bool(buffer) and (
                len(buffer) >= size_cap or (now - last_flush) >= interval
            )
            # Respect circuit breaker: skip flush if breaker is open
            if should_flush and _auto_save_circuit_open():
                _log_structured(
                    "warning",
                    "auto_save_circuit_breaker_skip",
                    buffer_size=len(buffer),
                    circuit_state="open",
                )
                # Record skipped entries as circuit-breaker skips
                for entry in buffer:
                    _record_circuit_skip(entry)
                buffer = []
                last_flush = now
                continue
            if should_flush:
                batch_size = len(buffer)
                flush_start = time.time()
                summary = _process_inbox_batch(buffer)
                flush_duration_ms = int((time.time() - flush_start) * 1000)
                _log_structured(
                    "info",
                    "auto_save_batch_flush",
                    batch_size=batch_size,
                    saved=summary["saved"],
                    skipped=summary["skipped"],
                    failed=summary["failed"],
                    duration_ms=flush_duration_ms,
                )
                buffer = []
                last_flush = now
                continue  # immediately try to drain more

            # 5. Wait for inbox activity using modification watcher (inotify/kqueue)
            # instead of busy-wait polling.
            timeout = interval
            if buffer:
                # If we have buffered entries, cap wait at remaining interval
                remaining = interval - (time.time() - last_flush)
                if remaining <= 0:
                    timeout = 0
                else:
                    timeout = min(timeout, remaining)

            inbox_path = get_auto_save_inbox_path()
            _wait_for_file_modification(inbox_path, timeout)
            if _DAEMON_STOP_REQUESTED:
                break
    finally:
        # Final flush so no entry is lost on shutdown.
        if buffer:
            try:
                summary = _process_inbox_batch(buffer)
                _log_structured(
                    "info",
                    "auto_save_final_flush",
                    batch_size=len(buffer),
                    saved=summary["saved"],
                    skipped=summary["skipped"],
                    failed=summary["failed"],
                )
            except Exception as e:
                _log_structured("warning", "auto_save_final_flush_failed", error=str(e))
        _remove_pid_file()
        _unregister_from_daemon_manifest()
        try:
            # Release the flock FD.  The FD close itself also releases
            # the flock (POSIX semantics), so we drop it from the
            # keep-alive dict first to avoid double-release.
            fd = _DAEMON_LOCKS.pop("auto_save_daemon", None)
            if fd is not None:
                try:
                    release_flock(fd)
                except Exception:
                    pass
                try:
                    fd.close()
                except Exception:
                    pass
        except Exception:
            pass
        _log_structured("info", "auto_save_daemon_stopped_stopped")
