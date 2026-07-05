#!/usr/bin/env python3
"""Inbox management and daemon lifecycle for auto-save.

Extracted from auto_save.py in Phase 3.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AUTO_SAVE_INBOX_FILENAME = ".auto_save_inbox.jsonl"
AUTO_SAVE_PID_FILENAME = ".auto_save_daemon.pid"
AUTO_SAVE_LOCK_FILENAME = ".auto_save_daemon.lock"
AUTO_SAVE_MANIFEST_FILENAME = ".auto_save_daemon_manifest.json"
AUTO_SAVE_RESULTS_FILENAME = ".auto_save_results.jsonl"
_DEFAULT_INBOX_MAX_BYTES = 100 * 1024 * 1024  # 100 MB



def _get_memory_dir() -> Path:
    from background.auto_save import get_db_path
    return get_db_path().parent


def get_auto_save_inbox_path() -> Path:
    """Path to the JSONL inbox used by the async auto-save daemon.

    The inbox lives next to the DB so it follows the same
    workspace-vs-global resolution as the memory store itself.
    """
    return _get_memory_dir() / AUTO_SAVE_INBOX_FILENAME


def get_auto_save_results_path() -> Path:
    """Path to the JSONL result file written by the auto-save daemon.

    Each line is a JSON object for one processed inbox entry:
    ``{ts, tool, note_id, status, error?, note_path?}``.
    The TS plugin and agent can read this file to get a lightweight
    callback signal beyond the fire-and-forget hook contract.
    """
    return _get_memory_dir() / AUTO_SAVE_RESULTS_FILENAME


def write_auto_save_result(
    tool: str,
    note_id: str,
    status: str,
    error: str = "",
    note_path: str = "",
    entry_id: str = "",
) -> None:
    """Append a single result entry to the daemon's result file."""
    path = get_auto_save_results_path()
    try:
        entry = {
            "ts": int(time.time() * 1000),
            "tool": tool,
            "note_id": note_id,
            "status": status,
            "error": error,
            "note_path": note_path,
            "entry_id": entry_id,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug("auto-save daemon: failed to write result file: %s", exc)


def get_auto_save_result(entry_id: str) -> dict | None:
    """Return the most recent result for a given entry_id, or None."""
    path = get_auto_save_results_path()
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            try:
                entry: dict[str, Any] = json.loads(line)
                if entry.get("entry_id") == entry_id:
                    return entry
            except Exception:
                continue
    except Exception:
        pass
    return None


def get_auto_save_pid_path() -> Path:
    """Path to the daemon's PID file.  Used for liveness detection
    by ``_is_daemon_running``."""
    return _get_memory_dir() / AUTO_SAVE_PID_FILENAME


def get_auto_save_lock_path() -> Path:
    """Path to the daemon's flock file.  Held by the running daemon
    to ensure only one daemon processes the inbox at a time."""
    return _get_memory_dir() / AUTO_SAVE_LOCK_FILENAME


def get_auto_save_manifest_path() -> Path:
    """Path to the global daemon manifest (tracks all daemon PIDs across projects).

    Lives in the config root so it's a single file to check when
    cleaning up stale daemons across all memory dirs.
    """
    base = Path(os.environ.get("MEMORY_CONFIG_DIR", Path.home() / ".config" / "agentic-memory"))
    return base / AUTO_SAVE_MANIFEST_FILENAME

def _read_daemon_manifest() -> dict[str, dict]:
    """Read the global daemon manifest.

    Returns {memory_dir_key: {"pid": int, "host": str, "started": float}}.
    Returns empty dict if the manifest doesn't exist or is corrupt.
    """
    path = get_auto_save_manifest_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError, ValueError):
        pass
    return {}


def _write_daemon_manifest(manifest: dict[str, dict]) -> None:
    """Write the global daemon manifest atomically."""
    path = get_auto_save_manifest_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(manifest, indent=2))
        tmp.replace(path)
    except OSError:
        pass


def _register_in_daemon_manifest() -> None:
    """Register the current process in the global daemon manifest.

    Also purges any stale entries (PIDs no longer alive) to keep
    the manifest clean.
    """
    manifest = _read_daemon_manifest()
    my_key = str(_get_memory_dir().resolve())
    now = time.time()
    manifest[my_key] = {"pid": os.getpid(), "host": socket.gethostname(), "started": now}

    stale = []
    for key, info in manifest.items():
        pid = info.get("pid", 0)
        if pid <= 0 or pid == os.getpid():
            continue
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            stale.append(key)
    for key in stale:
        manifest.pop(key, None)

    _write_daemon_manifest(manifest)


def _unregister_from_daemon_manifest() -> None:
    """Remove the current daemon from the global manifest."""
    manifest = _read_daemon_manifest()
    my_key = str(_get_memory_dir().resolve())
    manifest.pop(my_key, None)
    _write_daemon_manifest(manifest)



def _inbox_max_bytes() -> int:
    # Priority: env var > TOML config > default
    env_val = os.environ.get("AUTO_SAVE_INBOX_MAX_BYTES")
    if env_val is not None:
        return int(env_val)
    try:
        from infra._lazy_imports import get_config

        cfg = get_config()
        return int(getattr(cfg, "auto_save_inbox_max_bytes", _DEFAULT_INBOX_MAX_BYTES))
    except Exception:
        return _DEFAULT_INBOX_MAX_BYTES

def _is_daemon_running() -> bool:
    """True if a live auto-save daemon process exists for this memory dir.

    Reads the PID file and checks the OS for the process.  Returns
    ``False`` if the PID file is missing, unreadable, contains a
    stale PID (process not found).
    """
    pid_path = get_auto_save_pid_path()
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # signal 0 = existence check
    except (OSError, ProcessLookupError):
        return False
    return True


def _write_pid_file() -> bool:
    """Write the current process PID to the daemon PID file.

    Returns ``True`` on success, ``False`` if the write fails.  The
    file is written atomically (write-to-temp + rename) so a
    concurrent reader never sees a half-written PID.
    """
    pid_path = get_auto_save_pid_path()
    try:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = pid_path.with_suffix(pid_path.suffix + ".tmp")
        tmp.write_text(f"{os.getpid()}\n")
        tmp.replace(pid_path)
        return True
    except Exception as e:
        logger.warning("auto-save daemon: failed to write PID file: %s", e)
        return False


def _remove_pid_file() -> None:
    """Best-effort PID file removal.  Idempotent — missing is fine."""
    try:
        get_auto_save_pid_path().unlink(missing_ok=True)
    except Exception:
        pass

def _enqueue_to_inbox(entry: dict) -> bool:
    """Append a single entry to the async auto-save inbox.

    The entry is JSON-serialised to one line and appended to the
    inbox file.  Single-write appends of small (<4KB) lines are
    atomic on POSIX filesystems, so concurrent enqueues from
    multiple subprocesses never interleave inside a line.

    P0-4 fix (2026-06-22): if the inbox is at or above
    ``AUTO_SAVE_INBOX_MAX_BYTES`` (default 100 MB), the enqueue is
    refused (returns False) so the caller falls back to the sync
    path.  This prevents a single rogue 10 MB tool result from
    filling the disk before the daemon can drain it.

    Returns ``True`` on success, ``False`` on size-cap violation or
    any other error (caller falls back to the sync path).
    """
    inbox = get_auto_save_inbox_path()
    try:
        inbox.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        # P0-4 fix: check the inbox size BEFORE writing.  ``stat`` is
        # cheaper than writing and rolling back.  We compare against
        # the post-this-write size, so we add len(line) to the current
        # size — the cap is "inbox file size after this enqueue".
        max_bytes = _inbox_max_bytes()
        current_size = 0
        if inbox.exists():
            try:
                current_size = inbox.stat().st_size
            except OSError as exc:
                logger.debug("auto-save daemon: cannot stat inbox %s: %s", inbox, exc)
                current_size = 0
        if current_size + len(line.encode("utf-8")) > max_bytes:
            logger.warning(
                "auto-save: inbox at %d bytes, refusing enqueue of %d bytes "
                "(cap is %d). Caller will fall back to sync path.",
                current_size,
                len(line.encode("utf-8")),
                max_bytes,
            )
            return False
        with open(inbox, "a", encoding="utf-8") as f:
            f.write(line)
        return True
    except Exception as e:
        logger.warning("auto-save: failed to enqueue to inbox: %s", e)
        return False

def _drain_inbox() -> list[dict]:
    """Atomically drain the inbox and return the parsed entries.

    P1-2 fix (2026-06-22): the previous read-then-truncate pattern
    had a race window where a SIGKILL between read and truncate
    (or a concurrent enqueue after read but before truncate) lost
    entries.  The fix uses the rename-and-process pattern:

      1. Atomically rename ``inbox`` → ``inbox.pending.{pid}``
      2. Read and parse the renamed file (entries are now safe
         even if the process dies)
      3. Keep the renamed file as a .pending file until the batch
         is successfully committed to the DB

    If the daemon crashes after step 2 but before committing the
    batch, the .pending file remains on disk.  The next daemon
    startup re-processes any orphaned .pending files, giving us
    at-least-once delivery.

    New enqueues go to the new (empty) ``inbox`` file, so they
    are never lost.  The renamed file's content is stable because
    no new entries are appended to it (they go to the new file).

    The pid suffix avoids races between two concurrent drainers
    (only one holds the flock, but defence in depth is cheap).

    If parsing fails on a line, the line is dropped (logged at
    warning level) so a single corrupt entry can never block the
    daemon.

    Returns the list of parsed entries.  The list may be empty.
    """
    inbox = get_auto_save_inbox_path()
    if not inbox.exists():
        return []
    # P1-2 + at-least-once fix: rename inbox to a per-pid pending file.
    # We do NOT delete it here.  The daemon's main loop deletes it after
    # the batch is successfully committed.  If the daemon crashes before
    # commit, the pending file survives and gets re-processed on restart.
    import os as _os

    pending = inbox.with_suffix(f"{inbox.suffix}.pending.{_os.getpid()}")
    try:
        inbox.rename(pending)
    except FileNotFoundError:
        # Inbox was deleted between exists() check and rename.
        # Nothing to drain.
        return []
    except Exception as e:
        logger.warning("auto-save daemon: failed to rename inbox: %s", e)
        return []
    entries: list[dict] = []
    try:
        raw = pending.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("auto-save daemon: failed to read inbox: %s", e)
        try:
            pending.unlink(missing_ok=True)
        except Exception:
            pass
        return []
    for ln, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception as e:
            logger.warning(
                "auto-save daemon: dropped malformed inbox line %d: %s", ln, e
            )
    # Keep the pending file.  The daemon's main loop deletes it after
    # the batch is committed.  If the daemon crashes, the pending file
    # remains and is re-processed on the next startup.
    return entries


def _reprocess_orphaned_pending_files(max_age_s: float = 30.0) -> int:
    """Re-process any orphaned .pending files left by a crashed daemon.

    Scans the auto-save directory for ``.auto_save_inbox.jsonl.pending.*``
    files older than ``max_age_s``.  For each orphaned file, the entries
    are appended back to the inbox so the current daemon can process
    them normally.  The pending file is then deleted.

    Returns the number of orphaned entries re-queued.
    """
    inbox = get_auto_save_inbox_path()
    try:
        pending_files = sorted(inbox.parent.glob(f"{inbox.name}.pending.*"))
    except Exception as e:
        logger.debug("auto-save daemon: failed to list pending files: %s", e)
        return 0
    cutoff = time.time() - max_age_s
    requeued = 0
    for pf in pending_files:
        try:
            if pf.stat().st_mtime > cutoff:
                continue  # Too recent; current daemon is likely handling it.
            raw = pf.read_text(encoding="utf-8")
            entries: list[dict] = []
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
            if not entries:
                pf.unlink(missing_ok=True)
                continue
            with open(inbox, "a", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            requeued += len(entries)
            pf.unlink(missing_ok=True)
            logger.info(
                "auto-save daemon: re-queued %d entries from orphaned %s",
                len(entries),
                pf.name,
            )
        except Exception as e:
            logger.debug("auto-save daemon: failed to re-process %s: %s", pf.name, e)
    return requeued

def _is_daemon_lock_held() -> bool:
    """True if the daemon's flock file is held by another process.

    Uses the daemon's own flock file as the liveness check rather than
    the PID file, because the PID file has a race window: between the
    PID file check and spawning a new daemon, the running daemon may
    not have written its PID yet (Python takes ~100-500ms to init).
    The daemon holds the flock immediately at startup (before writing
    its PID), so the flock is the authoritative liveness signal.

    Returns True if the flock can't be acquired (daemon is running),
    False if the flock is free (no daemon is running).
    """
    lock_path = get_auto_save_lock_path()
    if not lock_path.exists():
        return False
    lock_fd = None
    try:
        lock_fd = open(lock_path, "w", encoding="utf-8")
    except OSError as exc:
        logger.warning("auto-save daemon: cannot open lock file %s: %s", lock_path, exc)
        return False
    try:
        from infra.file_lock import acquire_flock_with_retry, release_flock

        got = acquire_flock_with_retry(lock_fd, max_attempts=1, nonblocking=True)
        if got:
            release_flock(lock_fd)
            return False
        return True
    except Exception:
        return True  # safe side: assume running
    finally:
        try:
            lock_fd.close()
        except Exception:
            pass

def _cleanup_stale_daemon_lock() -> bool:
    """If the daemon lock is held by a dead PID, release the stale lock.

    Returns ``True`` if a stale lock was cleaned up, ``False`` if
    the lock is legitimately held by a live daemon or doesn't exist.
    """
    lock_path = get_auto_save_lock_path()
    if not lock_path.exists():
        return False
    # Check the PID file — if PID is dead, the lock is stale.
    pid_path = get_auto_save_pid_path()
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return False  # PID is alive — lock is legit
    except (OSError, ProcessLookupError):
        pass
    # PID is dead.  Try to acquire the lock; if we can't, the
    # lock is held by another process (unlikely since PID is dead,
    # but handle it defensively).
    lock_fd = None
    try:
        from infra.file_lock import acquire_flock_with_retry, release_flock

        lock_fd = open(lock_path, "w", encoding="utf-8")
        got = acquire_flock_with_retry(lock_fd, max_attempts=1, nonblocking=True)
        if got:
            release_flock(lock_fd)
            lock_fd.close()
            lock_fd = None
            # Stale lock cleaned — also remove the stale PID file.
            pid_path.unlink(missing_ok=True)
            return True
    except Exception:
        pass
    finally:
        if lock_fd is not None:
            try:
                lock_fd.close()
                lock_path.unlink(missing_ok=True)
            except Exception as e:
                logger.warning("auto-save: failed to clean stale lock: %s", e)
    return False

def _start_daemon_if_needed() -> bool:
    """Start the async auto-save daemon if it isn't already running.

    Spawns ``auto_save.py daemon`` as a detached background process.
    Returns ``True`` if the daemon was successfully launched.

    Uses the daemon's flock file as the primary liveness check (more
    reliable than the PID file: the flock is held immediately at
    daemon startup, before the PID is written, so there is no race
    window where we spawn a redundant daemon).

    If the lock is held but the daemon PID is dead (stale lock from
    a crashed daemon), the stale lock is cleaned up transparently
    and a new daemon is started.

    The detached process inherits the env (so MEMORY_DB_PATH, etc.
    stay in sync) and detaches from the parent's stdin/stdout/stderr
    so the opencode hook's fireAndForget doesn't keep a pipe to it.
    """
    if _is_daemon_running():
        return True
    # Clean up any stale lock from a dead daemon before checking
    # the live lock — otherwise a crashed daemon's lock file blocks
    # the spawn.
    _cleanup_stale_daemon_lock()
    # Double-check with flock: a daemon may be starting up but hasn't
    # written its PID yet.  The flock is held immediately at startup,
    # so it's the authoritative liveness signal.
    if _is_daemon_lock_held():
        return True
    script = Path(__file__).resolve().parent.parent / "auto_save.py"
    try:
        # Detach from the parent so the opencode hook doesn't block
        # waiting for the daemon's pipes to close.
        stdin_target = subprocess.DEVNULL
        stdout_target = subprocess.DEVNULL
        stderr_target = subprocess.DEVNULL
        subprocess.Popen(  # noqa: S603
            [sys.executable, str(script), "daemon"],
            stdin=stdin_target,
            stdout=stdout_target,
            stderr=stderr_target,
            start_new_session=True,
            env=os.environ.copy(),
        )
        return True
    except Exception as e:
        logger.warning("auto-save daemon: failed to spawn: %s", e)
        return False

def _process_inbox_batch(entries: list[dict]) -> dict:
    from background.tool_complete import _tool_complete_inner  # noqa: E402
    from background.auto_save import get_db_path  # noqa: E402
    """Process a batch of inbox entries synchronously.

    Used both by the daemon's main loop and by the inline fallback
    path (when the daemon is unavailable).  Each entry is passed
    through the standard allowlist/denylist/injection-scan pipeline
    and then saved via ``_tool_complete_inner``.

    Low-value entries are skipped before processing to reduce noise
    in the sessions category: empty/error-only result previews and
    repetitive tool calls within a short window are coalesced.

    Returns a summary dict: ``{"saved": N, "skipped": M, "failed": K}``.
    """
    summary = {"saved": 0, "skipped": 0, "failed": 0}
    if not entries:
        return summary

    from infra.db_write_queue import sqlite_write_queue

    db_path = get_db_path()
    conn = None
    try:
        conn = sqlite_write_queue.start_session(db_path)
    except Exception as e:
        logger.warning(
            "auto-save daemon: failed to acquire DB connection for batch: %s", e
        )
        conn = None

    try:
        for entry in entries:
            tool = entry.get("tool", "")
            params = entry.get("params", "")
            result_preview = entry.get("result_preview", "")
            ts = entry.get("ts")

            # Quality gate: skip low-value auto-saves before hitting the DB.
            # This cuts session-note bloat without losing intentional saves.
            preview_trimmed = (result_preview or "").strip()
            if not preview_trimmed:
                summary["skipped"] += 1
                continue
            if tool in ("bash", "run_command") and len(preview_trimmed) < 20:
                summary["skipped"] += 1
                continue
            error_indicators = ("Traceback (most recent call last)", "Error:", "Exception:", "FATAL", "CRITICAL", "command not found", "No such file or directory", "Permission denied")
            if any(preview_trimmed.startswith(ei) for ei in error_indicators):
                summary["skipped"] += 1
                continue

            try:
                result = _tool_complete_inner(
                    tool, params, result_preview, ts, conn=conn
                )
                if conn is not None and (
                    result.get("saved")
                    and not isinstance(result["saved"], str)
                    or (
                        isinstance(result["saved"], str)
                        and not result["saved"].startswith("Error")
                    )
                ):
                    try:
                        conn.commit()
                    except Exception as commit_err:
                        logger.warning(
                            "auto-save daemon: batch commit failed for entry: %s",
                            commit_err,
                        )
            except Exception as e:
                logger.warning("auto-save daemon: entry failed: %s", e)
                summary["failed"] += 1
                write_auto_save_result(
                    tool=tool or "unknown",
                    note_id="",
                    status="failed",
                    error=str(e),
                    entry_id=entry.get("entry_id", ""),
                )
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                continue
            if result.get("saved"):
                summary["saved"] += 1
                write_auto_save_result(
                    tool=tool,
                    note_id=result.get("note_id", ""),
                    status="saved",
                    note_path=result.get("path", ""),
                    entry_id=entry.get("entry_id", ""),
                )
            elif result.get("skipped"):
                summary["skipped"] += 1
            else:
                summary["failed"] += 1
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return summary
