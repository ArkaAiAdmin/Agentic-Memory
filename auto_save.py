#!/usr/bin/env python3
"""Auto-save hook + daily digest for Agentic Memory.

Two CLI subcommands:

  auto-save tool-complete --tool NAME --params JSON --result-preview TEXT
    Captures one tool invocation as a markdown note in memory/sessions/auto-*.
    Designed to be called from an opencode hook after every tool execution.

  auto-save daily-digest [--date YYYY-MM-DD]
    Rolls all auto-*.md notes for a given date into a single
    memory/sessions/YYYY-MM-DD.md note, then moves the auto-saves to
    memory/sessions/archive/auto-YYYY-MM-DD/.

  auto-save status
    Counts auto-*.md notes from the last 24h, last 7d, and per day.
    Used to verify the hook is firing.

Why this exists
---------------
The "remember to memory_save at end of session" contract is a
human-failure mode. This hook makes it automatic: every tool call
becomes a tiny memory note. The daily digest then summarises those
into a single coherent note per day.

Hook wiring:
  The plugin ``ecc-hooks.ts`` (``tool.execute.after`` event) calls this
  script as a subprocess for tools on the allowlist:

    fireAndForget(venvPython, [autoSavePy, "tool-complete", "--tool", input.tool, ...], "auto-save")

  The allowlist check USED to happen inside this file (``_resolve_allowlist``).
  It still does as a safety belt, but the primary gate is now in
  ``ecc-hooks.ts`` to avoid wasted subprocess spawns for non-allowlisted
  tools (40/48 tools skipped at the plugin level).

Or via the MCP tool directly: agent calls memory_auto_save_hook with
the same args.

Limitations
-----------
- Result previews are truncated to 200 chars to keep notes small.
- Params are JSON-serialised; non-JSON-serialisable params are skipped.
- The hook runs synchronously; for high-throughput sessions consider
  batching (not implemented; the per-tool write is ~5ms).
"""

import argparse
import json
import os
import re
import sys
import shutil
import time
import datetime
import logging
import sqlite3
import subprocess
import threading
from pathlib import Path
from typing import Optional, Any, Protocol

# H9 fix (2026-06-22): serialize `daily-digest` against a manual
# invocation of the same subcommand. The other subcommands
# (tool-complete, status, health-check) are read-only or
# write-once-per-call and don't need the lock.
sys.path.insert(0, str(Path(__file__).resolve().parent / "cron"))
from _flock import acquire_lock_or_exit  # noqa: E402  # type: ignore[import]

# H1 fix: configure root logging (idempotent).
from memory_common import (
    configure_logging,
    atomic_write,
    safe_close_db,
    connection_pool,
    _resolve_tags,
)

# H1 fix: hook path now invalidates the search cache so the canonical-path
# safety contract (every save clears _search_cache) is upheld here too.
try:
    from cache import _search_cache
except ImportError:  # cache module is optional in some test contexts
    _search_cache = None  # type: ignore[assignment]

configure_logging()
logger = logging.getLogger(__name__)


# Typed wrappers for Linux-only inotify syscalls (absent from macOS stubs).
# Single point of suppression: the getattr call itself. Call sites below
# are clean — mypy sees them as normal function calls.
class _InotifyInit(Protocol):
    def __call__(self) -> int: ...


class _InotifyAddWatch(Protocol):
    def __call__(self, fd: int, pathname: str, mask: int) -> int: ...


_inotify_init: _InotifyInit | None = getattr(os, "inotify_init", None)
_inotify_add_watch: _InotifyAddWatch | None = getattr(os, "inotify_add_watch", None)


# Structured logging helper for observability
def _log_structured(level: str, event: str, **fields: Any) -> None:
    """Emit a structured JSON log entry for observability."""
    import json as _json

    log_entry = {"event": event, **fields}
    getattr(logger, level)(_json.dumps(log_entry))


from memory_config import GLOBAL_MEM_DIR

ARCHIVE_DIR_NAME = "archive"

# Fallback constants for when config is not available
_DEFAULT_BATCH_INTERVAL_S = 0.5  # 500ms
_DEFAULT_BATCH_SIZE = 50
_DEFAULT_DAEMON_IDLE_S = 3600  # 1 hour
_DEFAULT_INBOX_MAX_BYTES = 100 * 1024 * 1024  # 100 MB
_DEFAULT_PREVIEW_MAX = 200
_DEFAULT_PARAMS_MAX = 2000
_DEFAULT_ASYNC_AUTOSAVE = True
_DEFAULT_HEALTH_CHECK_MINUTES = 5

AUTO_SAVE_INBOX_FILENAME = ".auto_save_inbox.jsonl"
AUTO_SAVE_PID_FILENAME = ".auto_save_daemon.pid"
AUTO_SAVE_LOCK_FILENAME = ".auto_save_daemon.lock"


def _batch_interval() -> float:
    from _lazy_imports import get_config

    return getattr(
        get_config(), "auto_save_batch_interval_seconds", _DEFAULT_BATCH_INTERVAL_S
    )


def _daemon_idle_seconds() -> int:
    from _lazy_imports import get_config

    return getattr(
        get_config(), "auto_save_daemon_idle_seconds", _DEFAULT_DAEMON_IDLE_S
    )


def _preview_max() -> int:
    from _lazy_imports import get_config

    return getattr(get_config(), "auto_save_preview_max", _DEFAULT_PREVIEW_MAX)


def _params_max() -> int:
    from _lazy_imports import get_config

    return getattr(get_config(), "auto_save_params_max", _DEFAULT_PARAMS_MAX)


def _health_check_minutes() -> int:
    from _lazy_imports import get_config

    return getattr(
        get_config(), "auto_save_health_check_minutes", _DEFAULT_HEALTH_CHECK_MINUTES
    )


# 2026-06-15: tool-call denylist for the auto-save hook.
# These tools fire constantly during normal agent operation but the resulting
# auto-* notes are pure tool-call co-occurrences that add no semantic value
# (they show up in the KG as the highest-edge entities and dominate the index
# without surfacing in any useful search). Override with
# ``AUTO_SAVE_TOOL_DENYLIST`` (comma-separated). Set to ``""`` to disable.
# Note: the user's *intentional* memory_save calls are NOT in this list —
# they pass the explicit save path, not this hook.
# 2026-06-22: allow-list for the auto-save hook.
# Only tools on this list get auto-saved. Everything else is silently
# skipped. This replaces the old denylist approach — instead of
# blocking specific noisy tools, we only save tools that produce
# lasting knowledge. Override with ``AUTO_SAVE_TOOL_ALLOWLIST``
# (comma-separated). Set to ``"*"`` to allow all tools (denylist-only mode).
DEFAULT_TOOL_ALLOWLIST = frozenset(
    {
        # Explicit save operations (user intent)
        "memory_save",
        "memory_supersede",
        "memory_delete",
        # Task tracking
        "todowrite",
        # Subagent task results
        "task",
        # User decisions
        "question",
        # File writes (code/content creation)
        "write",
        "edit",
    }
)

DEFAULT_TOOL_DENYLIST = frozenset(
    {
        # filesystem navigation — pure metadata, no content
        "filesystem_list_allowed_directories",
        "filesystem_list_directory",
        "filesystem_directory_tree",
        "filesystem_read_multiple_files",
        "filesystem_search_files",
        "filesystem_get_file_info",
        "filesystem_list_directory_with_sizes",
        # session lifecycle tools
        "memory_session_start",
        "memory_user_profile",
        "memory_recall_context",
        "memory_profile_access",
        "memory_record_ctr_feedback",
        "memory_check_concept_drift",
        # internal agent plumbing
        "todo",
        "process",
        "read_terminal",
    }
)


def _tool_name_matches(name: str, candidates: frozenset) -> bool:
    """Check if ``name`` matches any entry in ``candidates``.

    MCP tools are namespaced as ``<server>_<tool>`` (e.g.
    ``agentic-memory_memory_save``) while the allowlist/denylist uses
    bare tool names. This helper matches both forms."""
    if name in candidates:
        return True
    if "_" in name:
        unprefixed = name.split("_", 1)[1]
        if unprefixed in candidates:
            return True
    return False


def _resolve_denylist() -> frozenset:
    """Return the active tool denylist, honoring env/TOML override."""
    # Priority: env var > TOML config > default
    override = os.environ.get("AUTO_SAVE_TOOL_DENYLIST", "").strip()
    if override == "":
        if "AUTO_SAVE_TOOL_DENYLIST" in os.environ:
            return frozenset()
        # Fall back to TOML config
        try:
            from _lazy_imports import get_config

            cfg = get_config()
            toml_denylist = getattr(cfg, "auto_save_denylist", "")
            if toml_denylist:
                return frozenset(
                    t.strip() for t in toml_denylist.split(",") if t.strip()
                )
        except Exception:
            pass
        return DEFAULT_TOOL_DENYLIST
    if override.lower() in {"0", "false", "off", "disable", "disabled"}:
        return frozenset()
    return frozenset(t.strip() for t in override.split(",") if t.strip())


def _resolve_allowlist() -> frozenset | None:
    """Return the active tool allow-list, or None if all tools are allowed.

    Priority: env var > TOML config > default.
    Set to ``"*"`` to allow all tools (fall back to denylist-only).

    NOTE: allowlist takes precedence over denylist. When the allowlist
    is set (default), the denylist is never reached."""
    override = os.environ.get("AUTO_SAVE_TOOL_ALLOWLIST", "").strip()
    if override == "*":
        return None
    if override:
        return frozenset(t.strip() for t in override.split(",") if t.strip())
    # Fall back to TOML config
    try:
        from _lazy_imports import get_config

        cfg = get_config()
        toml_allowlist = getattr(cfg, "auto_save_allowlist", "")
        if toml_allowlist:
            return frozenset(t.strip() for t in toml_allowlist.split(",") if t.strip())
    except Exception:
        pass
    return DEFAULT_TOOL_ALLOWLIST


def get_db_path() -> Path:
    """Return the active memory DB — resolves to local workspace if available."""
    env = os.environ.get("MEMORY_DB_PATH")
    if env:
        return Path(env)
    from infrastructure import resolve_active_memory_dir

    return resolve_active_memory_dir() / "memory.db"


def _get_sessions_dir() -> Path:
    """Return the sessions directory for the active memory DB."""
    return get_db_path().parent / "sessions"


def _get_memory_dir() -> Path:
    """Return the memory root directory (parent of the DB file)."""
    return get_db_path().parent


def get_auto_save_inbox_path() -> Path:
    """Path to the JSONL inbox used by the async auto-save daemon.

    The inbox lives next to the DB so it follows the same
    workspace-vs-global resolution as the memory store itself.
    """
    return _get_memory_dir() / AUTO_SAVE_INBOX_FILENAME


def get_auto_save_pid_path() -> Path:
    """Path to the daemon's PID file.  Used for liveness detection
    by ``_is_daemon_running``."""
    return _get_memory_dir() / AUTO_SAVE_PID_FILENAME


def get_auto_save_lock_path() -> Path:
    """Path to the daemon's flock file.  Held by the running daemon
    to ensure only one daemon processes the inbox at a time."""
    return _get_memory_dir() / AUTO_SAVE_LOCK_FILENAME


def _async_autosave_enabled() -> bool:
    """True if the async/background-batch auto-save path is enabled.

    Toggle via ``MEMORY_ASYNC_AUTOSAVE=0`` to opt out and force the
    legacy inline path.  Defaults to enabled since 2026-06-22 — the
    async path is strictly faster (lower per-call latency) and at
    least as safe (inbox is append-only, daemon is restartable).
    """
    # Priority: env var > TOML config > default
    env_val = os.environ.get("MEMORY_ASYNC_AUTOSAVE")
    if env_val is not None:
        return env_val != "0"
    try:
        from _lazy_imports import get_config

        cfg = get_config()
        return getattr(cfg, "auto_save_async_enabled", _DEFAULT_ASYNC_AUTOSAVE)
    except Exception:
        return _DEFAULT_ASYNC_AUTOSAVE


def _batch_interval_s() -> float:
    # Priority: env var > TOML config > default
    env_val = os.environ.get("AUTO_SAVE_BATCH_INTERVAL")
    if env_val is not None:
        return float(env_val)
    try:
        from _lazy_imports import get_config

        cfg = get_config()
        return float(
            getattr(cfg, "auto_save_batch_interval_seconds", _DEFAULT_BATCH_INTERVAL_S)
        )
    except Exception:
        return _DEFAULT_BATCH_INTERVAL_S


def _batch_size() -> int:
    # Priority: env var > TOML config > default
    env_val = os.environ.get("AUTO_SAVE_BATCH_SIZE")
    if env_val is not None:
        return int(env_val)
    try:
        from _lazy_imports import get_config

        cfg = get_config()
        return int(getattr(cfg, "auto_save_batch_size", _DEFAULT_BATCH_SIZE))
    except Exception:
        return _DEFAULT_BATCH_SIZE


def _daemon_idle_s() -> float:
    # Priority: env var > TOML config > default
    env_val = os.environ.get("AUTO_SAVE_DAEMON_IDLE_S")
    if env_val is not None:
        return float(env_val)
    try:
        from _lazy_imports import get_config

        cfg = get_config()
        return float(
            getattr(cfg, "auto_save_daemon_idle_seconds", _DEFAULT_DAEMON_IDLE_S)
        )
    except Exception:
        return _DEFAULT_DAEMON_IDLE_S


def _inbox_max_bytes() -> int:
    # Priority: env var > TOML config > default
    env_val = os.environ.get("AUTO_SAVE_INBOX_MAX_BYTES")
    if env_val is not None:
        return int(env_val)
    try:
        from _lazy_imports import get_config

        cfg = get_config()
        return int(getattr(cfg, "auto_save_inbox_max_bytes", _DEFAULT_INBOX_MAX_BYTES))
    except Exception:
        return _DEFAULT_INBOX_MAX_BYTES


def _slugify(text: str, max_len: int = 80) -> str:
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "-", text.strip().lower())[:max_len]
    s = s.strip("-")
    return s or "item"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _truncate(s: str, n: int) -> str:
    s = str(s)
    if len(s) <= n:
        return s
    return s[:n] + "..."


# ---------------------------------------------------------------------------
# Async/background-batch infrastructure (2026-06-22)
# ---------------------------------------------------------------------------


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
            except OSError:
                # If we can't stat, allow the write and let the write
                # fail naturally if there's a deeper filesystem issue.
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

      1. Atomically rename ``inbox`` → ``inbox.processing.{pid}``
      2. Read and parse the renamed file (entries are now safe
         even if the process dies)
      3. Delete the renamed file when done

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
    # P1-2 fix: rename inbox to a per-pid temp file.  This is
    # atomic on POSIX, so no entries can be lost between read and
    # rename.  New enqueues go to the new (empty) inbox.
    import os as _os

    processing = inbox.with_suffix(f"{inbox.suffix}.processing.{_os.getpid()}")
    try:
        inbox.rename(processing)
    except FileNotFoundError:
        # Inbox was deleted between exists() check and rename.
        # Nothing to drain.
        return []
    except Exception as e:
        logger.warning("auto-save daemon: failed to rename inbox: %s", e)
        return []
    entries: list[dict] = []
    try:
        raw = processing.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("auto-save daemon: failed to read inbox: %s", e)
        try:
            processing.unlink(missing_ok=True)
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
    # Delete the processing file.  Entries are now safely in our
    # in-memory buffer; even if the daemon crashes here, the worst
    # case is the next drain re-reads these entries (the daemon
    # is idempotent on note_id via save_pipeline.upsert_row's
    # ON CONFLICT clause).
    try:
        processing.unlink(missing_ok=True)
    except Exception as e:
        logger.warning("auto-save daemon: failed to delete processing file: %s", e)
    return entries


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
    except OSError:
        if lock_fd is not None:
            try:
                lock_fd.close()
            except Exception:
                logger.warning(
                    "Failed to close lock fd during _is_daemon_running cleanup"
                )
        return False
    try:
        from file_lock import acquire_flock_with_retry, release_flock

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


def _start_daemon_if_needed() -> bool:
    """Start the async auto-save daemon if it isn't already running.

    Spawns ``auto_save.py daemon`` as a detached background process.
    Returns ``True`` if the daemon was successfully launched.

    Uses the daemon's flock file as the primary liveness check (more
    reliable than the PID file: the flock is held immediately at
    daemon startup, before the PID is written, so there is no race
    window where we spawn a redundant daemon).

    The detached process inherits the env (so MEMORY_DB_PATH, etc.
    stay in sync) and detaches from the parent's stdin/stdout/stderr
    so the opencode hook's fireAndForget doesn't keep a pipe to it.
    """
    if _is_daemon_running():
        return True
    # Double-check with flock: a daemon may be starting up but hasn't
    # written its PID yet.  The flock is held immediately at startup,
    # so it's the authoritative liveness signal.
    if _is_daemon_lock_held():
        return True
    script = Path(__file__).resolve()
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
    """Process a batch of inbox entries synchronously.

    Used both by the daemon's main loop and by the inline fallback
    path (when the daemon is unavailable).  Each entry is passed
    through the standard allowlist/denylist/injection-scan pipeline
    and then saved via ``_upsert_memory``.

    Returns a summary dict: ``{"saved": N, "skipped": M, "failed": K}``.
    """
    summary = {"saved": 0, "skipped": 0, "failed": 0}
    if not entries:
        return summary

    from db import connection_pool

    db_path = get_db_path()
    conn = None
    try:
        conn = connection_pool.get(str(db_path), timeout=30.0)
        conn.execute("PRAGMA busy_timeout = 30000;")
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
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                continue
            if result.get("saved"):
                summary["saved"] += 1
            elif result.get("skipped"):
                summary["skipped"] += 1
            else:
                summary["failed"] += 1
    finally:
        if conn is not None:
            from memory_common import safe_close_db

            safe_close_db(conn, should_commit=False)
    return summary


def _wait_for_file_modification(file_path: Path, timeout: float) -> None:
    """Wait for file or directory changes using kqueue or inotify, fallback to sleep."""
    if timeout <= 0:
        return

    # 1. Try kqueue (macOS / BSD)
    try:
        import select as _select

        if hasattr(_select, "kqueue"):
            kq = _select.kqueue()
            dir_fd = os.open(str(file_path.parent), os.O_RDONLY)
            kevents = [
                _select.kevent(
                    dir_fd,
                    filter=_select.KQ_FILTER_VNODE,
                    flags=_select.KQ_EV_ADD | _select.KQ_EV_CLEAR,
                    fflags=_select.KQ_NOTE_WRITE,
                )
            ]
            file_fd = None
            if file_path.exists():
                try:
                    file_fd = os.open(str(file_path), os.O_RDONLY)
                    kevents.append(
                        _select.kevent(
                            file_fd,
                            filter=_select.KQ_FILTER_VNODE,
                            flags=_select.KQ_EV_ADD | _select.KQ_EV_CLEAR,
                            fflags=_select.KQ_NOTE_WRITE | _select.KQ_NOTE_EXTEND,
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
        wd_dir = _inotify_add_watch(fd, str(file_path.parent), mask_dir)
        wd_file = None
        if file_path.exists():
            try:
                wd_file = _inotify_add_watch(fd, str(file_path), 0x2)
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


def _async_enqueue_or_fallback(
    tool: str, params: str, result_preview: str, ts: Optional[str]
) -> dict:
    """Async path: enqueue to the inbox and start the daemon if needed.

    Returns a "queued" envelope on success, or invokes the inline
    fallback (and returns its result) if the daemon can't be
    started or the inbox can't be written.

    The note_id is computed up-front so the caller can log/audit
    it before the actual save happens in the daemon.
    """
    ts = ts or _now_iso()
    ts_compact = ts.replace(":", "-").replace("T", "_").split(".")[0]
    tool_slug = _slugify(tool, max_len=40)
    note_id = f"sessions/auto-{ts_compact}-{tool_slug}"

    # Best-effort: ensure the daemon is alive.  If the spawn fails
    # we fall through to the sync path.
    if not _is_daemon_running():
        _start_daemon_if_needed()

    entry = {
        "ts": ts,
        "tool": tool,
        "params": params,
        "result_preview": result_preview,
    }
    if _enqueue_to_inbox(entry):
        return {
            "saved": "queued",
            "note_id": note_id,
            "path": str(_get_sessions_dir() / f"auto-{ts_compact}-{tool_slug}.md"),
            "timestamp": ts,
        }
    # Fallback: the inbox write failed.  Run the sync path so the
    # caller's data isn't lost.
    try:
        return _tool_complete_inner(tool, params, result_preview, ts)
    except Exception as e:
        return {
            "saved": False,
            "error": f"save failed: {e}",
            "note_id": note_id,
        }


def _upsert_memory(
    note_id: str,
    source_file: str,
    content: str,
    tags_json: list[str] | str | None,
    now_iso: str,
    pinned: int = 0,
    importance: int = 1,
    conn=None,
) -> bool:
    """Insert or update a memory note in the active DB via save_pipeline.save_memory.

    Delegates to the canonical save path so the hook path benefits from:
    - Input validation (_validate_save_params)
    - Saga crash consistency (saga_save_memory)
    - Write lock (flock)
    - Post-save hooks (contradiction check, audit, skill extraction, cache invalidation)
    """
    db = get_db_path()
    if not db.exists():
        return False
    try:
        parts = note_id.split("/", 1)
        category = parts[0] if len(parts) == 2 else "sessions"
        title_slug = parts[1] if len(parts) == 2 else note_id
        if tags_json is None:
            tags_list = []
        elif isinstance(tags_json, str):
            try:
                tags_list = json.loads(tags_json)
            except json.JSONDecodeError:
                tags_list = [
                    t.strip() for t in re.split(r"[,; ]+", tags_json) if t.strip()
                ]
        elif isinstance(tags_json, list):
            tags_list = [str(t).strip() for t in tags_json if t]
        else:
            tags_list = []

        from _lazy_imports import save_memory as _save_memory

        result = _save_memory(
            content=content,
            category=category,
            title_slug=title_slug,
            tags=tags_list,
            pinned=bool(pinned),
            is_global=False,
            safety_wiring=False,
            _now_iso=now_iso,
            importance=importance,
            _conn=conn,
            note_id=note_id,
        )
        return isinstance(result, str) and not result.startswith("Error")
    except Exception as e:
        # CRITICAL log — this represents data loss (saga raised,
        # memory was not persisted). Unlike the soft failure path
        # (saved=False with no exception), a hard failure MUST be
        # surfaced to the caller so the agent can react.
        logger.critical("DATA LOSS: failed to upsert memory %s: %s", note_id, e)
        raise  # Re-raise so tool_complete() handles retry + error surfacing


# ---------------------------------------------------------------------------
# Subcommand: tool-complete
# ---------------------------------------------------------------------------


# Module-level backoff / circuit-breaker state for tool_complete().
# When a save raises (DB locked, schema mismatch, disk full, etc.) we
# don't want to retry immediately on the next tool call — that becomes
# a hot loop that spams the DB and burns CPU. Instead we:
#   1. Track consecutive failures (window: auto_save_failure_window_seconds)
#   2. Wait with exponential backoff between attempts (base * 2^n, capped)
#   3. Trip a circuit breaker once we exceed max_retries within the window
#   4. Skip subsequent saves until the circuit resets
# State is process-local; for the opencode MCP server (one long-lived
# process) this gives us per-process protection. Multi-process safety
# is not needed — only one auto-save process is spawned per opencode
# instance.
from typing import TypedDict


class _AutoSaveState(TypedDict):
    failure_times: list[float]
    circuit_open_until: float
    last_backoff_seconds: float


_AUTO_SAVE_STATE: _AutoSaveState = {
    "failure_times": [],
    "circuit_open_until": 0.0,
    "last_backoff_seconds": 0.0,
}
_AUTO_SAVE_STATE_LOCK = threading.Lock()

# Module-level keep-alive for the daemon's flock FD.  Mirrors
# ``cron/_flock.py``: holding the FD in a module dict prevents the
# GC from closing it (which would release the flock and let a second
# daemon start).
_DAEMON_LOCKS: dict[str, Any] = {}

# Module-level stop flag for graceful shutdown.  The signal handler
# sets this; the main loop checks it each iteration.  Using a
# module-level bool (rather than a threading.Event) means callers
# that don't construct an event still get a working shutdown path
# — the CLI just calls ``run_daemon()`` with no args and SIGTERM
# still terminates cleanly.
_DAEMON_STOP_REQUESTED = False


def _update_shared_memory_state() -> None:
    """S1 (2026-06-23): mirror the in-process circuit breaker state
    into the shared memory segment, so CLI hooks can read it
    without a DB hit.

    Called by the daemon whenever the circuit state changes (open
    transition, close transition, or periodic refresh). Silently
    no-ops if shared memory is disabled, unavailable, or the
    segment doesn't exist (no daemon has created it yet).

    The function is a "best-effort" push — failures are logged at
    debug level but never raised. A failure here would just mean
    CLI hooks fall back to the DB audit log on their next call.
    """
    import os as _os
    import time as _t

    if _os.environ.get("MEMORY_USE_SHARED_MEMORY", "0") != "1":
        return
    try:
        import shared_memory_state as _sms

        state = _sms.SharedMemoryState()
        if not state.attach():
            return
        try:
            with _AUTO_SAVE_STATE_LOCK:
                state.write_state(
                    circuit_open_until=_AUTO_SAVE_STATE["circuit_open_until"],
                    failure_count=len(_AUTO_SAVE_STATE["failure_times"]),
                    last_backoff_seconds=_AUTO_SAVE_STATE["last_backoff_seconds"],
                    daemon_pid=_os.getpid(),
                    daemon_started_at=_t.time(),  # approximate
                    is_daemon_alive=True,
                )
        finally:
            state.detach()
    except Exception as _e:  # noqa: BLE001
        logger.debug("auto-save: shared memory update failed: %s", _e)


def _auto_save_circuit_open() -> bool:
    """True if the circuit breaker is currently open (skipping saves).

    S1 (2026-06-23): fast-path via shared memory. The CLI hook
    process checks the shared-memory segment first (microseconds),
    and falls back to the in-process state (loaded from the DB
    audit log on module init) if the segment is unavailable. This
    keeps the hot path sub-millisecond even when the DB is busy.
    """
    import os as _os
    import time as _t

    # Opt-in: only use shared memory if explicitly enabled. Default
    # off for backward compat (the DB audit log fallback is already
    # fast enough for most workloads).
    if _os.environ.get("MEMORY_USE_SHARED_MEMORY", "0") == "1":
        try:
            import shared_memory_state as _sms

            state = _sms.SharedMemoryState()
            if state.attach():
                try:
                    # is_circuit_open returns None if invalid, or
                    # True/False. If None, fall through to the
                    # in-process state.
                    result = state.is_circuit_open()
                    if result is not None:
                        return result
                finally:
                    state.detach()
        except Exception:
            # Shared memory unavailable (sandboxed env, file
            # descriptor exhausted, etc.) — fall through to
            # in-process state.
            pass

    with _AUTO_SAVE_STATE_LOCK:
        return _t.time() < _AUTO_SAVE_STATE["circuit_open_until"]


def _check_circuit_timeout_expiry() -> None:
    """Check if the circuit breaker timeout has expired and persist close.

    P0-11 fix (2026-06-23): detect the transition from open →
    closed (timeout expired) and persist the close event. Previously,
    the close event was only written when a save succeeded AFTER
    the circuit was tripped, but the circuit prevents saves from
    happening, so the close event was never persisted. Now we
    detect the transition explicitly.

    Called from tool_complete and the daemon loop. Idempotent — if
    the circuit is not open or the timeout hasn't expired, this is
    a no-op.
    """
    import time as _t

    with _AUTO_SAVE_STATE_LOCK:
        if (
            _AUTO_SAVE_STATE["circuit_open_until"] > 0
            and _t.time() >= _AUTO_SAVE_STATE["circuit_open_until"]
        ):
            _AUTO_SAVE_STATE["circuit_open_until"] = 0.0
            _AUTO_SAVE_STATE["failure_times"] = []
            should_persist = True
        else:
            should_persist = False
    if should_persist:
        _persist_circuit_state(
            "close",
            details={
                "reason": "timeout_expired",
                "recovered_at": _t.time(),
            },
        )
        # S1 (2026-06-23): mirror to shared memory.
        _update_shared_memory_state()


def _auto_save_record_failure_and_maybe_trip() -> dict:
    """Record a save failure. Returns the resolved backoff config.

    Side effects:
      - Appends the failure timestamp to the failure window
      - Computes the next backoff (exponential, capped)
      - Trips the circuit if max_retries exceeded in window
    """
    import time as _t

    try:
        from _lazy_imports import get_config

        cfg = get_config()
        max_retries = int(getattr(cfg, "auto_save_max_retries", 3))
        base = float(getattr(cfg, "auto_save_backoff_base_seconds", 1.0))
        cap = float(getattr(cfg, "auto_save_backoff_cap_seconds", 30.0))
        cb_seconds = float(getattr(cfg, "auto_save_circuit_breaker_seconds", 300.0))
        window = float(getattr(cfg, "auto_save_failure_window_seconds", 60.0))
    except Exception:
        max_retries, base, cap, cb_seconds, window = 3, 1.0, 30.0, 300.0, 60.0

    now = _t.time()
    cutoff = now - window
    transitioned_to_open = False
    open_until = 0.0
    with _AUTO_SAVE_STATE_LOCK:
        _AUTO_SAVE_STATE["failure_times"] = [
            t for t in _AUTO_SAVE_STATE["failure_times"] if t >= cutoff
        ]
        _AUTO_SAVE_STATE["failure_times"].append(now)

        n_failures = len(_AUTO_SAVE_STATE["failure_times"])
        next_backoff = min(cap, base * (2 ** max(0, n_failures - 1)))
        _AUTO_SAVE_STATE["last_backoff_seconds"] = next_backoff

        if n_failures > max_retries:
            prior_open = _AUTO_SAVE_STATE["circuit_open_until"]
            _AUTO_SAVE_STATE["circuit_open_until"] = now + cb_seconds
            # Detect a fresh transition to open (was 0 or already
            # expired, now in the future).  If the breaker is being
            # re-opened because it's still in cooldown from a previous
            # trip, we don't log a new open event — the prior one
            # already covers this window.
            if prior_open <= now:
                transitioned_to_open = True
                open_until = float(_AUTO_SAVE_STATE["circuit_open_until"])
        logger.error(
            "auto-save circuit breaker OPEN: %d failures in %.0fs window; "
            "skipping saves for %.0fs",
            n_failures,
            window,
            cb_seconds,
        )
    if transitioned_to_open:
        _persist_circuit_state(
            "open",
            details={
                "n_failures": n_failures,
                "window_s": window,
                "cb_seconds": cb_seconds,
                "open_until": open_until,
            },
        )
        # S1 (2026-06-23): mirror to shared memory if enabled.
        _update_shared_memory_state()

    return {
        "max_retries": max_retries,
        "next_backoff": next_backoff,
        "n_failures": n_failures,
        "circuit_breaker_seconds": cb_seconds,
    }


def _auto_save_record_success() -> None:
    """Reset backoff state on a successful save."""
    import time as _t

    was_open = False
    open_until_before = 0.0
    with _AUTO_SAVE_STATE_LOCK:
        # Capture whether the breaker was open BEFORE we clear failures.
        # If it was open and now we've had a success, the breaker has
        # effectively recovered (the next call passes the check in
        # _auto_save_circuit_open()).
        was_open = (
            _AUTO_SAVE_STATE["failure_times"] == []
            and _AUTO_SAVE_STATE["circuit_open_until"] > 0
        )
        open_until_before = float(_AUTO_SAVE_STATE["circuit_open_until"])
        _AUTO_SAVE_STATE["failure_times"] = []
        _AUTO_SAVE_STATE["last_backoff_seconds"] = 0.0
    # Do NOT reset circuit_open_until here — the breaker has its own
    # timeout. Resetting on success would defeat the purpose.
    if was_open:
        # Persist the close event so operators can see the recovery.
        _persist_circuit_state(
            "close",
            details={
                "open_until_was": open_until_before,
                "recovered_at": _t.time(),
            },
        )


def _record_circuit_skip(entry: dict) -> None:
    """Record a skipped entry due to circuit breaker being open."""
    try:
        from memory_common import open_db
        from db import connection_pool

        db_path = get_db_path()
        conn = connection_pool.get(str(db_path), timeout=5.0)
        try:
            conn.execute(
                "INSERT INTO memory_audit_log ("
                "  ts, tool, args, results_count, latency_ms"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    time.time(),
                    "auto_save_circuit_skip",
                    json.dumps(entry, default=str),
                    0,
                    0.0,
                ),
            )
            conn.commit()
        finally:
            try:
                from memory_common import safe_close_db

                safe_close_db(conn, should_commit=False)
            except Exception as e:
                logger.debug("Failed to close db connection in audit: %s", e)
    except Exception:
        pass


def _persist_circuit_state(event: str, *, details: dict) -> None:
    """Append a circuit-breaker event to ``memory_audit_log``.

    Audit-gap fix (2026-06-22 follow-up): the breaker state was only
    in-memory, so an operator had no record of past open/close
    transitions across process restarts.  Writing to the existing
    ``memory_audit_log`` table gives a queryable history without
    needing a new schema.

    Args:
        event: One of "open", "close", "half_open".
        details: A dict to JSON-encode into the ``args`` column.

    Failure mode: any error here is logged and swallowed.  The audit
    log is observability, not control flow — a failure to persist
    must never break the save path.
    """
    try:
        from memory_common import open_db
        from db import connection_pool

        db_path = get_db_path()
        conn = connection_pool.get(str(db_path), timeout=5.0)
        try:
            conn.execute(
                "INSERT INTO memory_audit_log ("
                "  ts, tool, args, results_count, latency_ms"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    time.time(),
                    f"auto_save_circuit_{event}",
                    json.dumps(details, default=str),
                    1,  # results_count: 1 = event recorded
                    0.0,  # latency_ms: not measured for background events
                ),
            )
            conn.commit()
        finally:
            try:
                from memory_common import safe_close_db

                safe_close_db(conn, should_commit=False)
            except Exception as exc:
                logger.debug(
                    "auto_save: safe_close_db during circuit-state persist failed (non-fatal): %s",
                    exc,
                )
    except Exception as exc:
        logger.debug(
            "auto_save: circuit-state persistence failed (non-fatal): %s",
            exc,
        )


def _auto_save_get_state() -> dict:
    """Return current backoff/breaker state (read-only copy)."""
    with _AUTO_SAVE_STATE_LOCK:
        state = {
            "failure_times": list(_AUTO_SAVE_STATE["failure_times"]),
            "circuit_open_until": _AUTO_SAVE_STATE["circuit_open_until"],
            "last_backoff_seconds": _AUTO_SAVE_STATE["last_backoff_seconds"],
        }
    state["circuit_open"] = _auto_save_circuit_open()
    return state


def _auto_save_reset_state() -> None:
    """Test helper: fully reset the backoff/breaker state."""
    with _AUTO_SAVE_STATE_LOCK:
        _AUTO_SAVE_STATE["failure_times"] = []
        _AUTO_SAVE_STATE["circuit_open_until"] = 0.0
        _AUTO_SAVE_STATE["last_backoff_seconds"] = 0.0


def _load_circuit_state_from_audit() -> None:
    """Load circuit breaker state from memory_audit_log on startup.

    Reads the most recent auto_save_circuit_open/close events and
    reconstructs the in-memory state so the breaker works across
    process restarts (CLI hooks and daemon share the same DB).
    """
    try:
        from memory_common import open_db
        from db import connection_pool

        db_path = get_db_path()
        conn = connection_pool.get(str(db_path), timeout=5.0)
        try:
            rows = conn.execute(
                """
                SELECT tool, args, ts
                FROM memory_audit_log
                WHERE tool IN ('auto_save_circuit_open', 'auto_save_circuit_close')
                ORDER BY ts DESC
                LIMIT 2
                """
            ).fetchall()

            if rows:
                latest_tool = rows[0][0]
                latest_args = json.loads(rows[0][1]) if rows[0][1] else {}
                now = time.time()

                with _AUTO_SAVE_STATE_LOCK:
                    if latest_tool == "auto_save_circuit_open":
                        open_until = latest_args.get("open_until", 0)
                        if open_until > now:
                            _AUTO_SAVE_STATE["circuit_open_until"] = open_until
                            # Reconstruct failure times from n_failures and window
                            n_failures = latest_args.get("n_failures", 3)
                            window = latest_args.get("window_s", 60.0)
                            _AUTO_SAVE_STATE["failure_times"] = [
                                now - window + i * (window / n_failures)
                                for i in range(n_failures)
                            ]
                        else:
                            # Circuit already closed — persist a close event
                            # so the audit trail always has a matching close
                            # for every open, even after a process restart.
                            _AUTO_SAVE_STATE["circuit_open_until"] = 0.0
                            _AUTO_SAVE_STATE["failure_times"] = []
                            _persist_circuit_state(
                                "close",
                                details={
                                    "reason": "timeout_expired_during_reload",
                                    "open_until_was": latest_args.get("open_until", 0),
                                    "recovered_at": time.time(),
                                },
                            )
                    elif latest_tool == "auto_save_circuit_close":
                        _AUTO_SAVE_STATE["circuit_open_until"] = 0.0
                        _AUTO_SAVE_STATE["failure_times"] = []
        finally:
            try:
                from memory_common import safe_close_db

                safe_close_db(conn, should_commit=False)
            except Exception as e:
                logger.debug("Failed to close db connection in load circuit: %s", e)
    except Exception:
        # If loading fails, start with clean state
        pass


# Load circuit breaker state on module import
_load_circuit_state_from_audit()


def _scan_content_for_injection(
    tool: str, params: str, result_preview: str
) -> dict | None:
    """Run the prompt-injection scan on the tool-derived content
    that auto_save is about to write to disk.

    Returns a rejection dict if the content is high-risk (risk_score
    >= 0.5); returns ``None`` to allow the save to proceed (clean
    content OR scan failure).

    H-fix 2026-06-22: previously auto_save wrote tool content
    directly to disk without the injection scan. The scan was only
    in save_pipeline. This means a tool that bypasses the canonical
    save path (e.g. raw ``write`` tool that an agent could call) could
    persist injection-style content. The scan is pure, deterministic,
    and fast (regex over params + result_preview) so adding it here
    doesn't add a hot-path cost.

    Per the contract in save_memory._scan_for_injection_or_skip:
      * risk_score >= 0.5 → hard reject (never written)
      * risk_score > 0     → allowed but tier=untrusted (set downstream)
      * risk_score == 0    → clean, allow
    """
    from _lazy_imports import scan_for_injection

    content_to_scan = " ".join(filter(None, [params, result_preview]))
    if not content_to_scan.strip():
        return None
    try:
        scan = scan_for_injection(content_to_scan)
    except Exception as e:
        logger.debug("auto_save: injection scan failed (benign): %s", e)
        return None

    risk = float(scan.get("risk_score", 0.0))
    is_suspicious = bool(scan.get("is_suspicious", False))
    if risk >= 0.5:
        logger.warning(
            "auto_save: REJECTED injection-suspicious tool content "
            "(tool=%s risk=%.2f matches=%s)",
            tool,
            risk,
            scan.get("matches", []),
        )
        return {
            "saved": False,
            "skipped": True,
            "reason": "high_risk_prompt_injection",
            "tool": tool,
            "risk_score": risk,
            "matches": scan.get("matches", []),
        }
    if is_suspicious:
        logger.info(
            "auto_save: low-risk injection patterns in tool=%s "
            "(risk_score=%.2f) — allowing save with quarantine metadata",
            tool,
            risk,
        )
    return None


def _tool_complete_inner(
    tool: str, params: str, result_preview: str, ts: Optional[str], conn=None
) -> dict:
    """The original tool_complete body, factored out so retry can wrap it.

    Performs the file write + DB upsert. Returns the save result dict,
    or raises on hard failure (which the retry wrapper converts to a
    backoff/circuit-breaker event).
    """
    if not tool:
        raise ValueError("empty tool name")
    allowlist = _resolve_allowlist()
    if allowlist is not None and not _tool_name_matches(tool, allowlist):
        return {
            "saved": False,
            "skipped": True,
            "reason": "tool not in allowlist",
            "tool": tool,
        }
    denylist = _resolve_denylist()
    if _tool_name_matches(tool, denylist):
        return {
            "saved": False,
            "skipped": True,
            "reason": "tool on denylist",
            "tool": tool,
        }
    # H-fix 2026-06-22: scan tool-derived content for prompt
    # injection. Hard-reject high-risk (>=0.5); allow but tag the
    # quarantine metadata for low-risk. Scan failure is non-fatal
    # (we never block a save on a scan error, only on a positive hit).
    injection_check = _scan_content_for_injection(tool, params, result_preview)
    if injection_check is not None:
        return injection_check
    ts = ts or _now_iso()
    ts_compact = ts.replace(":", "-").replace("T", "_").split(".")[0]
    tool_slug = _slugify(tool, max_len=40)
    note_id = f"sessions/auto-{ts_compact}-{tool_slug}"
    filename = f"{note_id}.md"
    target_dir = _get_sessions_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / f"auto-{ts_compact}-{tool_slug}.md"

    try:
        if params:
            params_obj = json.loads(params)
            params_str = json.dumps(params_obj, indent=2, ensure_ascii=False)
        else:
            params_obj = None
            params_str = "_no params_"
    except (json.JSONDecodeError, TypeError):
        params_obj = None
        params_str = _truncate(params, _params_max()) if params else "_no params_"

    if len(params_str) > _params_max():
        params_str = params_str[: _params_max()] + "..."

    result_str = _truncate(result_preview or "_no result preview_", _preview_max())

    markdown = f"""---
created: {ts}
updated: {ts}
observed_at: {ts}
tags: [auto-save, hook, tool-log, {tool_slug}]
pinned: false
related: []
valid_from: {ts}
valid_to: null
superseded_by: null
---

# Auto-save: {tool} @ {ts_compact}

**Tool**: `{tool}`
**Timestamp**: {ts}

## Params
```json
{params_str}
```

## Result (preview)
{result_str}

---
*Auto-generated by auto_save.py. Will be rolled into `sessions/{ts[:10]}.md` by the daily digest.*
"""
    atomic_write(file_path, markdown, encoding="utf-8")

    tags = _resolve_tags("sessions", None, context="auto-save", tool_slug=tool_slug)
    saved = _upsert_memory(
        note_id,
        f"sessions/{file_path.name}",
        markdown,
        tags,  # Pass list directly, no JSON double-encoding
        ts,
        pinned=0,
        importance=1,
        conn=conn,
    )
    return {
        "saved": saved,
        "note_id": note_id,
        "path": str(file_path),
        "timestamp": ts,
    }


def tool_complete(
    tool: str, params: str, result_preview: str = "", ts: Optional[str] = None
) -> dict:
    """Save one tool invocation as a session note, with backoff + circuit
    breaker on failure.

    Returns a dict with 'saved' (bool), 'note_id' (str), 'path' (str), and
    on failure 'error', 'backoff_seconds', and possibly 'circuit_open'.

    Behavior on failure:
      - First failure: return immediately with error + backoff_seconds
        so the next attempt waits
      - After max_retries failures within failure_window_seconds: open
        the circuit breaker; subsequent calls return
        ``{"saved": False, "circuit_open": True}`` for
        circuit_breaker_seconds.

    A "soft" failure (saved=False with no exception) is treated as a
    caller-visible error: the result dict is returned as-is and the
    backoff state is NOT updated. A "hard" failure (raised exception)
    triggers the backoff + circuit breaker.
    """
    # P0-11 fix (2026-06-23): check for circuit timeout expiry before
    # checking the open state. This ensures the close event is
    # persisted when the circuit transitions from open to closed.
    _check_circuit_timeout_expiry()
    if _auto_save_circuit_open():
        return {
            "saved": False,
            "skipped": True,
            "reason": "circuit_breaker_open",
            "circuit_open_until": _AUTO_SAVE_STATE["circuit_open_until"],
        }
    # Async/background-batch path (2026-06-22).  When enabled, the
    # actual save runs in a long-running daemon.  The hook returns
    # immediately with a "queued" envelope, so per-call latency drops
    # from ~100-200ms (Python subprocess + sync work) to ~2-5ms
    # (just the inbox append + daemon liveness check).
    #
    # The fast path handles allowlist/denylist/injection at enqueue
    # time, so the daemon doesn't have to re-filter — the entry
    # already passed the gates.  A failure to enqueue (or to start
    # the daemon) falls through to the inline sync path so no
    # data is lost.
    if _async_autosave_enabled():
        async_envelope = _fast_path_enqueue(tool, params, result_preview, ts)
        if async_envelope is not None:
            return async_envelope
        # Fall through to the sync path on enqueue/daemon failure.
    try:
        result = _tool_complete_inner(tool, params, result_preview, ts)
    except Exception as e:
        cb = _auto_save_record_failure_and_maybe_trip()
        logger.warning(
            "auto-save %s failed: %s (failure %d/%d within window, backoff=%.1fs)",
            tool,
            e,
            cb["n_failures"],
            cb["max_retries"] + 1,
            cb["next_backoff"],
        )
        return {
            "saved": False,
            "error": f"save failed: {e}",
            "backoff_seconds": cb["next_backoff"],
            "n_failures": cb["n_failures"],
            "circuit_open": _auto_save_circuit_open(),
        }
    if result.get("saved"):
        _auto_save_record_success()
    return result


def _fast_path_enqueue(
    tool: str, params: str, result_preview: str, ts: Optional[str]
) -> Optional[dict]:
    """Apply the gates (allowlist/denylist/injection) and enqueue if they pass.

    Returns the "queued" envelope on success, or ``None`` if any
    step fails (caller falls through to the sync path).  The gate
    checks are intentionally duplicated here so the daemon can be
    a pure writer — it never has to re-validate.
    """
    try:
        if not tool:
            return None
        allowlist = _resolve_allowlist()
        if allowlist is not None and not _tool_name_matches(tool, allowlist):
            return {
                "saved": False,
                "skipped": True,
                "reason": "tool not in allowlist",
                "tool": tool,
            }
        denylist = _resolve_denylist()
        if _tool_name_matches(tool, denylist):
            return {
                "saved": False,
                "skipped": True,
                "reason": "tool on denylist",
                "tool": tool,
            }
        # Run the injection scan on the fast path too — the daemon
        # trusts the entry and does not re-scan.  A high-risk hit
        # must block the save at the hook.
        injection_check = _scan_content_for_injection(tool, params, result_preview)
        if injection_check is not None:
            return injection_check
        return _async_enqueue_or_fallback(tool, params, result_preview, ts)
    except Exception as e:
        # Any unexpected failure on the fast path must not block the
        # save — fall through to the sync path.
        logger.debug("auto-save: fast-path enqueue failed, falling back: %s", e)
        return None


# ---------------------------------------------------------------------------
# Subcommand: daily-digest
# ---------------------------------------------------------------------------


def _build_daily_sections(
    autos: list[Path], date_str: str
) -> tuple[list[str], list[tuple[Path, str, str]], dict[str, int]]:
    """Walk the auto-save files for ``date_str`` and return:

      * sections: the rendered markdown sections (one per file)
      * path_meta: parallel list of (path, ts_part, tool_slug)
      * tool_counts: tool-slug → count

    The C7 fix lives here: filenames like
    ``auto-{date_str}_{HH-MM-SS}-{tool_slug}.md`` have dashes inside
    the timestamp, so a greedy regex would mis-extract the
    tool_slug. Non-greedy ``(.+?)`` anchored to the last dash before
    ``.md`` is the durable fix.

    Extracted 2026-06-22 from daily_digest().
    """
    from concurrent.futures import ThreadPoolExecutor

    filename_re = re.compile(rf"auto-{re.escape(date_str)}_(.+?)-([^-]+)\.md")

    # First, extract metadata from filenames (fast, no I/O)
    path_meta: list[tuple[Path, str, str]] = []
    tool_counts: dict[str, int] = {}
    for path in autos:
        m = filename_re.match(path.name)
        if m:
            ts_part, tool_slug = m.group(1), m.group(2)
        else:
            ts_part, tool_slug = "unknown", "unknown"
        path_meta.append((path, ts_part, tool_slug))
        tool_counts[tool_slug] = tool_counts.get(tool_slug, 0) + 1

    # Then read file bodies in parallel (I/O bound)
    def read_body(path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="replace")

    with ThreadPoolExecutor(max_workers=4) as executor:
        bodies = list(executor.map(read_body, autos))

    # Build sections from the parallel-read bodies
    sections: list[str] = []
    for (path, ts_part, tool_slug), body in zip(path_meta, bodies):
        ts_match = re.search(r"\*\*Timestamp\*\*: (\S+)", body)
        result_match = re.search(r"## Result \(preview\)\n(.*?)\n---", body, re.DOTALL)
        result_text = result_match.group(1).strip() if result_match else "_no preview_"
        sections.append(
            f"### {ts_part} — `{tool_slug}`\n"
            f"_{ts_match.group(1) if ts_match else ''}_\n\n"
            f"```\n{_truncate(result_text, 200)}\n```"
        )
    return sections, path_meta, tool_counts


def _get_tool_counts_from_db(date_str: str) -> dict[str, int]:
    """Get tool breakdown from database for a given date.

    More efficient than parsing filenames - uses SQL directly on the
    memories table.
    """
    try:
        from db import connection_pool

        db_path = get_db_path()
        conn = connection_pool.get(str(db_path), timeout=10.0)
        try:
            # Extract tool slug from source_file which has format:
            # sessions/auto-YYYY-MM-DD_HH-MM-SS-tool_slug.md
            rows = conn.execute(
                """
                SELECT 
                    CASE 
                        WHEN source_file LIKE '%+00-00-%' 
                        THEN substr(source_file, 41, length(source_file) - 43)
                        ELSE substr(source_file, 35, length(source_file) - 37)
                    END as tool_slug,
                    COUNT(*) as cnt
                FROM memories
                WHERE id LIKE 'sessions/auto-%' 
                  AND source_file LIKE 'sessions/auto-' || ? || '_%'
                  AND deleted_at IS NULL
                GROUP BY tool_slug
                """,
                (date_str,),
            ).fetchall()
            return {row[0]: row[1] for row in rows}
        finally:
            from memory_common import safe_close_db

            safe_close_db(conn, should_commit=False)
    except Exception:
        return {}


def _archive_one_autosave(
    path: Path, ts_part: str, tool_slug: str, date_str: str, archive_dir: Path
) -> bool:
    """C9 fix: delete the DB row FIRST (idempotent — re-runs are
    safe), then move the file. The previous order (move-then-delete)
    left a window where the file was archived but the DB row leaked.

    Extracted 2026-06-22 from daily_digest().
    """
    note_id = f"sessions/auto-{date_str}_{ts_part}-{tool_slug}"
    try:
        conn = connection_pool.get(str(get_db_path()), timeout=10.0)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            rowid = conn.execute(
                "SELECT rowid FROM memories WHERE id = ?", (note_id,)
            ).fetchone()
            if rowid:
                # Only manually delete from FTS5 if the content-sync
                # trigger is NOT present. With the trigger, DELETE FROM
                # memories automatically removes the FTS5 row.
                trigger_exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND name='memories_ad'"
                ).fetchone()
                if not trigger_exists:
                    conn.execute(
                        "DELETE FROM memories_fts WHERE rowid = ?", (rowid[0],)
                    )
            conn.execute("DELETE FROM memories WHERE id = ?", (note_id,))
            fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk_violations:
                logger.warning(
                    "FK violations after daily-digest DELETE: %s",
                    fk_violations[:5],
                )
            conn.commit()
        finally:
            safe_close_db(conn)
    except Exception as e:
        logger.warning("could not delete archived DB row for %s: %s", path.name, e)
    # Now move the file (idempotent: if missing, skip silently).
    try:
        if path.exists():
            shutil.move(str(path), str(archive_dir / path.name))
            return True
    except Exception as e:
        logger.warning("could not move %s: %s", path.name, e)
    return False


def _sweep_orphan_rows() -> None:
    """Sweep pre-existing orphan rows in tables that don't have
    ``ON DELETE CASCADE`` or that pre-date ``PRAGMA foreign_keys=ON``.
    Catches rows in user_access_log, memory_embeddings,
    memory_chunks, memory_vec_keys, kg_facts that reference
    deleted memories.

    Extracted 2026-06-22 from daily_digest().
    """
    try:
        conn = connection_pool.get(str(get_db_path()), timeout=10.0)
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            try:
                for table, col in [
                    ("user_access_log", "note_id"),
                    ("memory_embeddings", "memory_id"),
                    ("memory_chunks", "parent_id"),
                    ("memory_vec_keys", "memory_id"),
                    ("kg_facts", "source_memory"),
                ]:
                    try:
                        conn.execute(
                            f"DELETE FROM {table} WHERE {col} NOT IN "
                            f"(SELECT id FROM memories)"
                        )
                    except Exception:
                        pass
                conn.commit()
            finally:
                # P0-2 fix (2026-06-24): wrap the restore in try/except so a
                # failure to re-enable foreign_keys doesn't leave the connection
                # with foreign_keys=OFF when it's returned to the pool.
                try:
                    conn.execute("PRAGMA foreign_keys=ON")
                except Exception:
                    logger.warning("Failed to restore PRAGMA foreign_keys=ON")
        finally:
            safe_close_db(conn)
    except Exception as e:
        logger.debug("daily-digest orphan cleanup skipped: %s", e)


def daily_digest(date_str: Optional[str] = None, dry_run: bool = False) -> dict:
    """Roll all auto-*.md notes for `date_str` into one sessions/YYYY-MM-DD.md.

    If date_str is None, defaults to yesterday (most common case: run at
    midnight to roll up the day that's just ended).

    Decomposed 2026-06-22 — 3 named helpers handle the section
    building, per-file archive move, and orphan sweep. The
    orchestrator below reads as a 5-step pipeline.
    """
    if date_str is None:
        date_str = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    # Validate
    try:
        datetime.date.fromisoformat(date_str)
    except ValueError:
        return {"digested": 0, "error": f"invalid date: {date_str}"}

    _get_sessions_dir().mkdir(parents=True, exist_ok=True)
    target_note_id = f"sessions/{date_str}"
    target_path = _get_sessions_dir() / f"{date_str}.md"

    # Find auto-saves for this date
    prefix = f"auto-{date_str}_"
    autos = sorted(_get_sessions_dir().glob(f"{prefix}*.md"))
    if not autos:
        return {"digested": 0, "date": date_str, "note": "no auto-saves found"}

    sections, path_meta, _ = _build_daily_sections(autos, date_str)

    # Use SQL for tool breakdown (more efficient than file parsing)
    tool_counts = _get_tool_counts_from_db(date_str)
    if not tool_counts:
        # Fallback to file parsing if SQL query returns empty
        _, _, tool_counts = _build_daily_sections(autos, date_str)

    tool_summary = ", ".join(
        f"`{k}`×{v}" for k, v in sorted(tool_counts.items(), key=lambda x: -x[1])
    )
    ts = _now_iso()
    daily_md = f"""---
created: {ts}
updated: {ts}
observed_at: {ts}
tags: [daily-digest, session-log, {date_str}]
pinned: false
related: []
valid_from: {ts}
valid_to: null
superseded_by: null
---

# Session Digest: {date_str}

**Auto-saves captured**: {len(autos)}
**Tool breakdown**: {tool_summary or "_none_"}

## Timeline

{chr(10).join(sections)}

---
*Auto-generated by auto_save.py daily-digest. Source auto-saves moved to `sessions/archive/auto-{date_str}/`.*
"""
    if dry_run:
        return {"digested": len(autos), "date": date_str, "preview": daily_md[:500]}

    atomic_write(target_path, daily_md, encoding="utf-8")

    archive_dir = _get_sessions_dir() / ARCHIVE_DIR_NAME / f"auto-{date_str}"
    archive_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for path, ts_part, tool_slug in path_meta:
        if _archive_one_autosave(path, ts_part, tool_slug, date_str, archive_dir):
            moved += 1

    _sweep_orphan_rows()

    _upsert_memory(
        target_note_id,
        f"sessions/{date_str}.md",
        daily_md,
        ["daily-digest", "session-log", date_str],
        ts,
        pinned=0,
        importance=2,
    )
    return {
        "digested": moved,
        "date": date_str,
        "note_id": target_note_id,
        "tool_breakdown": tool_counts,
    }


# ---------------------------------------------------------------------------
# Purge all auto-save tool-log entries
# ---------------------------------------------------------------------------


def purge_auto_saves(dry_run: bool = False) -> dict:
    """Delete all auto-saved tool-log entries from DB and disk.

    Queries the ``memories`` table for ``note_id LIKE 'sessions/auto-%'``,
    soft-deletes them, and moves the corresponding markdown files to
    ``sessions/archive/purged-{date}/``.

    This is a one-shot cleanup for the 3412+ zero-importance auto-save
    entries that accumulated before the allow-list was introduced.

    Returns a dict with counts of deleted DB rows and moved files.
    """
    db_path = get_db_path()
    if not db_path.exists():
        return {"error": "no database found", "deleted": 0}

    db = connection_pool.get(str(db_path), timeout=10.0)
    db.row_factory = sqlite3.Row
    try:
        # Find all auto-save note_ids
        rows = db.execute(
            "SELECT id, source_file FROM memories WHERE id LIKE 'sessions/auto-%' AND deleted_at IS NULL"
        ).fetchall()
        note_ids = [r["id"] for r in rows]
        source_files = [r["source_file"] for r in rows]

        if not note_ids:
            return {"deleted": 0, "message": "no auto-save entries found"}

        if dry_run:
            return {
                "dry_run": True,
                "would_delete": len(note_ids),
                "sample_ids": note_ids[:5],
            }

        # Soft-delete from DB
        now_ts = _now_iso()
        db.executemany(
            "UPDATE memories SET deleted_at = ? WHERE id = ?",
            [(now_ts, nid) for nid in note_ids],
        )
        db.commit()

        # Move markdown files to archive
        sessions_dir = _get_sessions_dir()
        archive_name = f"purged-{datetime.date.today().isoformat()}"
        archive_dir = sessions_dir / "archive" / archive_name
        archive_dir.mkdir(parents=True, exist_ok=True)

        moved = 0
        for sf in source_files:
            if not sf:
                continue
            src = sessions_dir / Path(sf).name
            if src.exists():
                shutil.move(str(src), str(archive_dir / src.name))
                moved += 1

        return {
            "deleted": len(note_ids),
            "files_moved": moved,
            "archive_dir": str(archive_dir),
        }
    finally:
        safe_close_db(db)


# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------


def status() -> dict:
    """Count auto-saves from last 48h, 7d, and per-day."""
    _get_sessions_dir().mkdir(parents=True, exist_ok=True)
    autos = sorted(_get_sessions_dir().glob("auto-*.md"))
    now = datetime.datetime.now()
    last_48h = 0
    last_7d = 0
    per_day: dict[str, int] = {}
    for path in autos:
        m = re.match(
            r"auto-(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})([+-]\d{2}-\d{2})?-",
            path.name,
        )
        if not m:
            continue
        d = datetime.datetime.fromisoformat(
            f"{m.group(1)} {m.group(2).replace('-', ':')}"
        )
        per_day[m.group(1)] = per_day.get(m.group(1), 0) + 1
        age = now - d
        if age < datetime.timedelta(hours=48):
            last_48h += 1
        if age <= datetime.timedelta(days=7):
            last_7d += 1
    return {
        "last_48h": last_48h,
        "last_7d": last_7d,
        "total": len(autos),
        "per_day": dict(sorted(per_day.items(), reverse=True)[:14]),
        "sessions_dir": str(_get_sessions_dir()),
    }


# ---------------------------------------------------------------------------
# Subcommand: health-check
# ---------------------------------------------------------------------------


def health_check(minutes: int = _DEFAULT_HEALTH_CHECK_MINUTES) -> dict:
    """Check auto-save pipeline health."""
    import time
    from pathlib import Path

    _get_sessions_dir().mkdir(parents=True, exist_ok=True)
    now = time.time()
    window = minutes * 60

    # Count recent auto-saves
    autos = sorted(_get_sessions_dir().glob("auto-*.md"))
    recent_autos = 0
    for path in autos:
        try:
            mtime = path.stat().st_mtime
            if now - mtime <= window:
                recent_autos += 1
        except OSError:
            pass

    # Check DB writability with read-only PRAGMA quick_check
    db_writable = False
    db_error = None
    try:
        from db import connection_pool

        db_path = get_db_path()
        conn = connection_pool.get(str(db_path), timeout=5.0)
        try:
            # Read-only integrity check - doesn't modify the database
            result = conn.execute("PRAGMA quick_check").fetchone()
            if result and result[0] == "ok":
                db_writable = True
            else:
                db_error = f"PRAGMA quick_check failed: {result}"
        finally:
            from memory_common import safe_close_db

            safe_close_db(conn, should_commit=False)
    except Exception as e:
        db_error = str(e)

    # Check state file
    state_file = _get_sessions_dir() / ".context_monitor_state.json"
    state_file_exists = state_file.exists()
    state_file_age = None
    if state_file_exists:
        try:
            state_file_age = now - state_file.stat().st_mtime
        except OSError:
            pass

    # Last compaction age
    last_compaction = None
    for path in sorted(_get_sessions_dir().glob("compaction-save-*.md"), reverse=True):
        try:
            last_compaction = now - path.stat().st_mtime
            break
        except OSError:
            pass

    # Hook failure count from error log
    hook_failure_count = 0
    error_log = GLOBAL_MEM_DIR / "hook-errors.jsonl"
    if error_log.exists():
        try:
            hook_failure_count = sum(1 for _ in error_log.open())
        except OSError:
            pass
        # P0-12 fix (2026-06-23): rotate hook-errors.jsonl if it
        # exceeds 10 MB. Without rotation, the file grows unbounded
        # and can hit inode limits. Rotation: rename to .1, .2, etc.
        # Keep last 3 rotations.
        try:
            max_size = 10 * 1024 * 1024  # 10 MB
            if error_log.stat().st_size > max_size:
                for i in range(3, 0, -1):
                    src = error_log.with_suffix(
                        f".jsonl.{i - 1}" if i > 1 else error_log.suffix
                    )
                    dst = error_log.with_suffix(f".jsonl.{i}")
                    if src.exists():
                        src.rename(dst)
                error_log.rename(error_log.with_suffix(".jsonl.1"))
        except OSError as exc:
            logger.warning(
                "auto-save daemon: hook-errors.jsonl rotation failed: %s", exc
            )

    return {
        "healthy": db_writable and recent_autos > 0,
        "auto_save_recent": recent_autos,
        "db_writable": db_writable,
        "db_error": db_error,
        "state_file_exists": state_file_exists,
        "state_file_age_sec": state_file_age,
        "last_compaction_age_sec": last_compaction,
        "hook_failure_count": hook_failure_count,
        "window_minutes": minutes,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(prog="auto_save")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_tc = sub.add_parser(
        "tool-complete", help="Save a tool invocation as a session note"
    )
    p_tc.add_argument("--tool", required=True, help="Tool name (e.g. memory_search)")
    p_tc.add_argument("--params", default="", help="JSON-serialised params")
    p_tc.add_argument("--result-preview", default="", help="Truncated result text")
    p_tc.add_argument("--ts", default=None, help="Override timestamp (ISO)")

    p_dd = sub.add_parser(
        "daily-digest", help="Roll up auto-saves for a date into a daily note"
    )
    p_dd.add_argument("--date", default=None, help="YYYY-MM-DD; default = yesterday")
    p_dd.add_argument("--dry-run", action="store_true")

    sub.add_parser("status", help="Count auto-saves")

    p_hc = sub.add_parser("health-check", help="Check auto-save pipeline health")
    p_hc.add_argument(
        "--minutes",
        type=int,
        default=_health_check_minutes(),
        help="Recent window in minutes",
    )

    sub.add_parser(
        "daemon",
        help="Long-running daemon that drains the async auto-save inbox",
    )

    args = p.parse_args()
    if args.cmd == "tool-complete":
        result = tool_complete(args.tool, args.params, args.result_preview, args.ts)
        # Print errors to stderr to avoid leaking into OpenCode TUI
        if result.get("error"):
            print(json.dumps(result), file=sys.stderr)
        else:
            print(json.dumps(result))
        sys.exit(0)
    elif args.cmd == "daemon":
        # Long-running process.  Uses SIGTERM/SIGINT to exit
        # cleanly; the parent's fireAndForget spawn never sees the
        # return value (it redirected stdout/stderr to /dev/null).
        run_daemon()
        sys.exit(0)
    elif args.cmd == "daily-digest":
        # H9 fix: serialize against a manual invocation so the
        # auto-roll at 00:00 and a user-triggered roll can never
        # both move the same auto-*.md files into the archive.
        acquire_lock_or_exit("auto_save_daily_digest")
        result = daily_digest(args.date, args.dry_run)
        print(json.dumps(result, indent=2))
        sys.exit(0)
    elif args.cmd == "status":
        result = status()
        print(json.dumps(result, indent=2))
        sys.exit(0)
    elif args.cmd == "health-check":
        result = health_check(args.minutes)
        print(json.dumps(result, indent=2))
        sys.exit(0)


if __name__ == "__main__":
    main()
