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
import socket
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

# C15 fix (2026-06-27): set OMP env vars early to prevent MPS kernel crashes
# when multiple subprocesses load sentence-transformers concurrently on Apple
# Silicon. These must be set before any module that loads OpenMP/torch.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

# H9 fix (2026-06-22): serialize `daily-digest` against a manual
# invocation of the same subcommand. The other subcommands
# (tool-complete, status, health-check) are read-only or
# write-once-per-call and don't need the lock.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cron"))
from _flock import acquire_lock_or_exit  # noqa: E402  # type: ignore[import]

# H1 fix: configure root logging (idempotent).
from memory_common import (
    configure_logging,
    atomic_write,
    safe_close_db,
    connection_pool,
    _resolve_tags,
)

# Phase 3: circuit breaker is now in its own module.
from background.circuit_breaker import (
    _AutoSaveState,
    _AUTO_SAVE_STATE,
    _AUTO_SAVE_STATE_LOCK,
    _DAEMON_LOCKS,
    _DAEMON_STOP_REQUESTED,
    _update_shared_memory_state,
    _auto_save_circuit_open,
    _check_circuit_timeout_expiry,
    _auto_save_record_failure_and_maybe_trip,
    _auto_save_record_success,
    _record_circuit_skip,
    _persist_circuit_state,
    _auto_save_get_state,
    _auto_save_reset_state,
    _load_circuit_state_from_audit,
)

# Phase 4: config and tool-policy helpers moved to background/config.py.
from background.config import (
    _batch_interval,
    _daemon_idle_seconds,
    _preview_max,
    _params_max,
    _health_check_minutes,
    _resolve_denylist,
    _resolve_allowlist,
    _tool_name_matches,
    DEFAULT_TOOL_ALLOWLIST,
    DEFAULT_TOOL_DENYLIST,
    _DEFAULT_BATCH_INTERVAL_S,
    _DEFAULT_BATCH_SIZE,
    _DEFAULT_DAEMON_IDLE_S,
    _DEFAULT_PREVIEW_MAX,
    _DEFAULT_PARAMS_MAX,
    _DEFAULT_HEALTH_CHECK_MINUTES,
    _DEFAULT_ASYNC_AUTOSAVE,
)



# H1 fix: hook path now invalidates the search cache so the canonical-path
# safety contract (every save clears _search_cache) is upheld here too.
try:
    from cache import _search_cache
except ImportError:  # cache module is optional in some test contexts
    _search_cache = None

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

_AUTO_SAVE_DEDUP_CACHE: dict[str, float] = {}
_AUTO_SAVE_DEDUP_TTL_S = 24 * 3600
_DEDUP_LOCK_STALE_S = 300  # 5 min — stale lock holder is assumed dead


def _get_dedup_lock_dir() -> Path:
    return _get_sessions_dir() / ".dedup_locks"


def _acquire_dedup_lock(key: str) -> bool:
    try:
        lock_dir = _get_dedup_lock_dir()
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_file = lock_dir / f"{key}.lock"
        fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        return False


def _release_dedup_lock(key: str) -> None:
    try:
        (_get_dedup_lock_dir() / f"{key}.lock").unlink(missing_ok=True)
    except OSError:
        pass


def _is_dedup_lock_stale(lock_path: Path) -> bool:
    try:
        age = time.time() - lock_path.stat().st_mtime
        return age > _DEDUP_LOCK_STALE_S
    except OSError:
        return True

def _auto_log_archive_dir() -> Path:
    return GLOBAL_MEM_DIR / "log-archive"

def _dedup_key(tool: str, params: str, result_preview: str) -> str:
    import hashlib
    raw = f"{tool}\0{params}\0{result_preview}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

def _should_skip_dedup(key: str) -> bool:
    now = time.time()
    last = _AUTO_SAVE_DEDUP_CACHE.get(key)
    if last is None:
        return False
    if now - last > _AUTO_SAVE_DEDUP_TTL_S:
        del _AUTO_SAVE_DEDUP_CACHE[key]
        return False
    return True

def _record_dedup(key: str) -> None:
    _AUTO_SAVE_DEDUP_CACHE[key] = time.time()

def _auto_save_ttl_hours() -> int:
    env_val = os.environ.get("AUTO_SAVE_TTL_HOURS")
    if env_val is not None:
        try:
            return max(1, int(env_val))
        except ValueError:
            pass
    return 24

def get_db_path() -> Path:
    """Return the active memory DB — resolves to local workspace if available."""
    env = os.environ.get("MEMORY_DB_PATH")
    if env:
        return Path(env)
    from infra.infrastructure import resolve_active_memory_dir

    return resolve_active_memory_dir() / "memory.db"


def _get_sessions_dir() -> Path:
    """Return the sessions directory for the active memory DB."""
    return get_db_path().parent / "sessions"


def _get_memory_dir() -> Path:
    """Return the memory root directory (parent of the DB file)."""
    return get_db_path().parent




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






























# ---------------------------------------------------------------------------
# Subcommand: daily-digest
# ---------------------------------------------------------------------------












# ---------------------------------------------------------------------------
# Purge all auto-save tool-log entries
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Phase 3: extracted modules.
from background.inbox import (
    get_auto_save_inbox_path,
    get_auto_save_pid_path,
    get_auto_save_lock_path,
    get_auto_save_manifest_path,
    _read_daemon_manifest,
    _write_daemon_manifest,
    _register_in_daemon_manifest,
    _unregister_from_daemon_manifest,
    _inbox_max_bytes,
    _is_daemon_running,
    _write_pid_file,
    _remove_pid_file,
    _enqueue_to_inbox,
    _drain_inbox,
    _is_daemon_lock_held,
    _cleanup_stale_daemon_lock,
    _start_daemon_if_needed,
    _process_inbox_batch,
)
from background.daemon import run_daemon, _wait_for_file_modification
from background.tool_complete import tool_complete
from background.daily_digest import daily_digest, _build_daily_sections
from background.purge import purge_auto_saves

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
        except OSError as exc:
            logger.debug("auto-save daemon: cannot stat session %s: %s", path, exc)

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
        except OSError as exc:
            logger.debug("auto-save daemon: cannot stat state file: %s", exc)

    # Last compaction age
    last_compaction = None
    for path in sorted(_get_sessions_dir().glob("compaction-save-*.md"), reverse=True):
        try:
            last_compaction = now - path.stat().st_mtime
            break
        except OSError as exc:
            logger.debug(
                "auto-save daemon: cannot stat compaction file %s: %s", path, exc
            )

    # Hook failure count from error log
    hook_failure_count = 0
    error_log = GLOBAL_MEM_DIR / "hook-errors.jsonl"
    if error_log.exists():
        try:
            hook_failure_count = sum(1 for _ in error_log.open())
        except OSError as exc:
            logger.debug("auto-save daemon: cannot count hook-errors.jsonl: %s", exc)
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
        default=_health_check_minutes,  # callable: argparse calls it only when --minutes absent
        help="Recent window in minutes",
    )

    sub.add_parser(
        "daemon",
        help="Long-running daemon that drains the async auto-save inbox",
    )

    p_ls = sub.add_parser(
        "list-daemons", help="List all registered auto-save daemons"
    )
    p_ls.add_argument(
        "--clean-stale",
        action="store_true",
        help="Remove stale entries (dead PIDs) from the manifest",
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
    elif args.cmd == "list-daemons":
        manifest = _read_daemon_manifest()
        if args.clean_stale:
            cleaned = 0
            for key, info in list(manifest.items()):
                pid = info.get("pid", 0)
                if pid > 0:
                    try:
                        os.kill(pid, 0)
                    except (OSError, ProcessLookupError):
                        manifest.pop(key)
                        cleaned += 1
            _write_daemon_manifest(manifest)
            print(json.dumps({"daemons": manifest, "cleaned_stale": cleaned}, indent=2))
        else:
            print(json.dumps(manifest, indent=2))
        sys.exit(0)



# Re-export extracted symbols for backward compatibility
from background.tool_complete import (
    _upsert_memory,
    _scan_content_for_injection,
    _tool_complete_inner,
    _fast_path_enqueue,
    _async_enqueue_or_fallback,
)
from background.daily_digest import (
    _get_tool_counts_from_db,
    _archive_one_autosave,
    _sweep_orphan_rows,
    _build_daily_sections,
    daily_digest,
)

if __name__ == "__main__":
    main()
