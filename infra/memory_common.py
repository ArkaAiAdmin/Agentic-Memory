"""Shared utilities for the Agentic Memory system.

This module is now a thin re-export shim after the 6-module refactor
(see ``db.py``, ``fts.py``, ``frontmatter.py``, ``file_lock.py``,
``memory_config.py``, ``safe_call.py``).

The original 1256-line monolith was split into the six focused modules
above. All public names that used to live here are re-exported so
``from memory_common import X`` continues to work for all 93 dependents.

Code that DID NOT move (still defined here):
  * ``atomic_write`` — generic filesystem helper, not DB-specific.
  * ``RateLimiter`` class + ``get_default_limiter`` / ``rate_limit_check``
    / ``reset_rate_limiter`` — process-wide throttling for MCP tools.

Migration helpers and ``run_db_migrations`` now live in ``db_migrations.py``
(imported from there and re-exported for backward compat).
"""

from __future__ import annotations

import functools
import json
import os
import re
import time
import warnings
from pathlib import Path
from typing import Optional, cast

from infra.frontmatter import _coerce  # noqa: F401
# Re-exports from the 6 new modules (one canonical home, multiple import paths)
import logging

from infra.db import (
    _ConnectionPool,
    connection_pool,
    safe_close_db,
    open_db,
    wal_checkpoint_idle,
    count_rows,
)
import infra.db as _db_module
from infra.fts import (
    cleanup_fts5_orphans,
    _migrate_fts5_porter_tokenizer,
    _migrate_ensure_fts_triggers,
)
from infra.frontmatter import parse_frontmatter
from infra.file_lock import (
    _try_flock,
    acquire_flock_with_retry,
    release_flock,
)
from infra.memory_config import (
    GLOBAL_MEM_DIR,
    get_memory_paths,
    find_project_root,
    configure_logging,
    log_backup,
    validate_config,
    PROJECT_ROOT_MARKERS,
    _VALID_LOG_LEVELS,
)
from infra.safe_call import safe_call

# Migration helpers — canonical home is db_migrations.py, re-exported here
# so ``from memory_common import _migrate_*`` continues to work.
from infra.db_migrations import (
    SCHEMA_VERSION,
    run_db_migrations,
    _migrate_schema_version,
    _migrate_memory_embeddings,
    _migrate_memory_audit_log,
    _migrate_memory_vec_idx,
    _migrate_ensure_columns,
    _migrate_ensure_backlinks_table,
    _migrate_ensure_indexes,
    _migrate_memory_ctr_feedback,
    _migrate_concept_drift,
    _migrate_ensure_chunks_table,
    _migrate_kg_tables,
    _migrate_kg_extraction_stats,
)


# Module-level __getattr__ to proxy module-global state (_STARTUP_CHECKPOINT_DONE,
# _startup_checkpoint_lock) to db.py so writes from db functions are visible via
# memory_common.X.  Module-level __setattr__ is bypassed by Python's module
# attribute machinery, so writes still go to memory_common.__dict__ — that's
# OK as long as callers always go through memory_common (which is what the
# _maybe_checkpoint_on_startup wrapper below ensures).
_PROXIED_DB_NAMES = frozenset({"_STARTUP_CHECKPOINT_DONE", "_startup_checkpoint_lock"})

# NB: _PROXIED_DB_NAMES must be kept in sync with db.py's module-level state vars.
# If you add one to db.py, add it here too — otherwise __getattr__ raises.


def __getattr__(name):
    if name in _PROXIED_DB_NAMES:
        return getattr(_db_module, name)
    # Fall through to _db_module for any state var that follows the naming
    # convention. This makes the proxy robust against future additions.
    if hasattr(_db_module, name):
        return getattr(_db_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals().keys()) | set(dir(_db_module)))


def _maybe_checkpoint_on_startup(path):
    """Wrapper that syncs _STARTUP_CHECKPOINT_DONE in both modules.

    Tests read the flag through memory_common.  db._maybe_checkpoint_on_startup
    uses ``global _STARTUP_CHECKPOINT_DONE`` which writes only to db.  This
    wrapper calls db's version and then copies the flag value back so
    ``memory_common._STARTUP_CHECKPOINT_DONE`` reflects the current truth.
    """
    globals()["_STARTUP_CHECKPOINT_DONE"] = getattr(
        _db_module, "_STARTUP_CHECKPOINT_DONE", False
    )
    _db_module._maybe_checkpoint_on_startup(path)
    globals()["_STARTUP_CHECKPOINT_DONE"] = _db_module._STARTUP_CHECKPOINT_DONE


__all__ = [
    # db.py
    "_ConnectionPool",
    "connection_pool",
    "safe_close_db",
    "open_db",
    "wal_checkpoint_idle",
    "count_rows",
    "_maybe_checkpoint_on_startup",
    # fts.py
    "cleanup_fts5_orphans",
    "_migrate_fts5_porter_tokenizer",
    "_migrate_ensure_fts_triggers",
    # frontmatter.py
    "parse_frontmatter",
    "_coerce",
    # file_lock.py
    "_try_flock",
    "acquire_flock_with_retry",
    "release_flock",
    # memory_config.py
    "GLOBAL_MEM_DIR",
    "get_memory_paths",
    "find_project_root",
    "configure_logging",
    "log_backup",
    "validate_config",
    "PROJECT_ROOT_MARKERS",
    "_VALID_LOG_LEVELS",
    # safe_call.py
    "safe_call",
    # db_migrations.py (re-exported for backward compat)
    "atomic_write",
    "run_db_migrations",
    "SCHEMA_VERSION",
    "_migrate_schema_version",
    "_migrate_memory_embeddings",
    "_migrate_memory_audit_log",
    "_migrate_memory_vec_idx",
    "_migrate_ensure_columns",
    "_migrate_ensure_backlinks_table",
    "_migrate_ensure_indexes",
    "_migrate_memory_ctr_feedback",
    "_migrate_concept_drift",
    "_migrate_ensure_chunks_table",
    "_migrate_kg_tables",
    "_migrate_kg_extraction_stats",
    # RateLimiter
    "RateLimiter",
    "get_default_limiter",
    "rate_limit_check",
    "reset_rate_limiter",
    # decorators
    "deprecated",
]


# ---------------------------------------------------------------------------
# atomic_write — generic filesystem helper (not DB-specific)
# ---------------------------------------------------------------------------


logger = logging.getLogger(__name__)


def atomic_write(path: Path, content: str | bytes, encoding: str = "utf-8") -> None:
    """Write `content` to `path` atomically.

    L4 fix: replaces the duplicated `temp = path.with_suffix('.md.tmp');
    temp.write_text(...); os.replace(temp, path)` pattern that lived in
    14 places across the codebase. Now there is one implementation, and
    adding fsync / backup rotation later only has to be done here.

    `content` may be either `str` (default) or `bytes`. The `encoding`
    parameter is only used in the `str` case; bytes are written verbatim.

    Behavior:
      1. Creates `path`'s parent directory if missing (idempotent).
      2. Writes to a sibling temp file: `path` with an extra `.tmp`
         suffix appended (so e.g. `foo.md` -> `foo.md.tmp`).
      3. Calls `os.replace(tmp, path)`, which is atomic on POSIX and
         on Windows when both paths are on the same volume.
      4. On any exception, the temp file is cleaned up best-effort.
    """
    path = Path(path)
    parent = path.parent
    if parent and (not parent.exists()):
        parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        if isinstance(content, bytes):
            tmp_path.write_bytes(content)
        else:
            tmp_path.write_text(content, encoding=encoding)
        # fsync to ensure data reaches disk before rename
        with open(tmp_path, "rb" if isinstance(content, bytes) else "r") as f:
            os.fsync(f.fileno())
        os.replace(str(tmp_path), str(path))
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            logger.warning(
                "Failed to clean up temp file %s after write error", tmp_path
            )
            pass
        raise


def safe_atomic_write(
    path: Path,
    content: str | bytes,
    *,
    encoding: str = "utf-8",
    expected_existing: str | bytes | None = None,
) -> tuple[bool, str | None]:
    """Atomic write with concurrent-edit detection (Scenario 4 fix).

    Like ``atomic_write``, but if ``expected_existing`` is provided
    and the on-disk file content does NOT match, the on-disk
    content is first saved as a conflict file
    (``<path>.conflict-<pid>-<ts>``) so the user can recover the
    "losing" version, then the new content is written.  This
    closes the LWW (last-writer-wins) gap where a concurrent edit
    would be silently overwritten.

    Without ``expected_existing`` this function is equivalent to
    ``atomic_write`` — i.e. it does a best-effort write with no
    detection.  The caller (typically the save-memory saga) knows
    what the file contained when the saga started, and passes that
    in via ``expected_existing``.

    Returns:
        ``(success, conflict_path_or_None)``.  ``conflict_path_or_None``
        is the path to the saved conflict file (if a conflict was
        detected), or ``None`` if no conflict.
    """
    path = Path(path)
    conflict_path_saved: str | None = None
    if expected_existing is not None and path.exists():
        current: str | bytes | None = None
        try:
            current = (
                path.read_text(encoding=encoding)
                if isinstance(content, str)
                else path.read_bytes()
            )
        except Exception as e:
            logger.warning(
                "safe_atomic_write: failed to read existing %s for conflict check: %s",
                path,
                e,
            )
            # current stays None — we fall through to the regular
            # atomic_write below (no conflict detection this time).
        if current is not None and current != expected_existing:
            import time as _time

            ts = int(_time.time())
            conflict_path = path.with_suffix(
                f"{path.suffix}.conflict-{os.getpid()}-{ts}"
            )
            try:
                if isinstance(current, str):
                    conflict_path.write_text(current, encoding=encoding)
                else:
                    conflict_path.write_bytes(current)
                logger.warning(
                    "safe_atomic_write: %s changed since expected; "
                    "previous content saved to %s",
                    path,
                    conflict_path,
                )
                conflict_path_saved = str(conflict_path)
            except Exception as e:
                logger.warning(
                    "safe_atomic_write: failed to save conflict file %s: %s",
                    conflict_path,
                    e,
                )
                # Fall through to the regular write — better to lose
                # the conflict copy than to lose the save entirely.
    atomic_write(path, content, encoding=encoding)
    return (True, conflict_path_saved)


# ---------------------------------------------------------------------------
# RateLimiter — process-wide throttling for MCP tools
# ---------------------------------------------------------------------------

import collections as _collections
import threading as _threading
import time as _time


class RateLimiter:
    """Thread-safe sliding-window rate limiter, keyed by name.

    Each call to ``check(name)`` returns True if the call is allowed
    under the configured ``max_calls`` per ``window_seconds`` and
    records the call's timestamp; False if the call would exceed the
    budget. Old timestamps are dropped on each check so the window
    slides forward.

    Per-tool limits can be supplied via ``per_tool_limits``: a dict of
    ``tool_name → (max_calls, window_seconds)``. When a tool has an
    entry there that entry overrides the class-level defaults.

    Args:
        max_calls: Default maximum calls allowed per window (default 60).
        window_seconds: Default window length in seconds (default 60).
        per_tool_limits: Per-tool overrides ``{name: (max_calls, window_seconds)}``.
    """

    def __init__(
        self,
        max_calls: int = 60,
        window_seconds: float = 60.0,
        per_tool_limits: dict[str, tuple[int, float]] | None = None,
    ):
        if max_calls <= 0:
            raise ValueError("max_calls must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.max_calls = max_calls
        self.window_seconds = float(window_seconds)
        self.per_tool_limits = per_tool_limits or {}
        self._buckets: dict = {}
        self._lock = _threading.Lock()

    def _get_limit(self, name: str) -> tuple[int, float]:
        if name in self.per_tool_limits:
            return self.per_tool_limits[name]
        return self.max_calls, self.window_seconds

    def check(self, name: str) -> bool:
        """Return True if a call to ``name`` is allowed right now.

        Side effect: records the current timestamp under ``name`` if
        the call is allowed. The call is rejected (returns False) if
        the recorded timestamps in the last ``window_seconds`` are
        already at the cap.
        """
        now = _time.monotonic()
        max_calls, window_seconds = self._get_limit(name)
        cutoff = now - window_seconds
        with self._lock:
            bucket = self._buckets.setdefault(name, _collections.deque())
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= max_calls:
                return False
            bucket.append(now)
            return True

    def reset(self, name: Optional[str] = None) -> None:
        """Clear the bucket for ``name`` (or all names if None).

        Useful in tests and for the operator who wants to manually
        unstick a hot tool.
        """
        with self._lock:
            if name is None:
                self._buckets.clear()
            else:
                self._buckets.pop(name, None)


_default_limiter: Optional[RateLimiter] = None
_default_limiter_lock = _threading.Lock()


def get_default_limiter() -> RateLimiter:
    """Return the process-wide default ``RateLimiter`` (lazy-initialized).

    Reads per-tool limits from ``get_config().rate_limits`` when
    available so operators get the configured defaults without extra code.
    """
    global _default_limiter
    with _default_limiter_lock:
        if _default_limiter is None:
            per_tool: dict[str, tuple[int, float]] = {}
            try:
                from config import get_config
                cfg = get_config()
                toml_limits = dict(cfg.rate_limits or {})
                try:
                    from infra.rate_limiter import _resolve_tool_limits, _default_limits
                    known_tools = set(_default_limits()) - {"_default"}
                    for tool in known_tools:
                        limits = _resolve_tool_limits(tool, toml_limits)
                        per_tool[tool] = (limits["burst"], 60.0)
                    # also populate the catch-all
                    _resolve_tool_limits("_default", toml_limits)
                except Exception:
                    for tool, entry in toml_limits.items():
                        if isinstance(entry, dict):
                            rpm = float(entry.get("rate", 60.0 / 60.0)) * 60.0
                            burst = int(entry.get("burst", max(1, int(rpm))))
                            per_tool[tool] = (burst, 60.0)
            except Exception:
                pass
            _default_limiter = RateLimiter(max_calls=60, window_seconds=60.0, per_tool_limits=per_tool)
        return _default_limiter


def rate_limit_check(name: str) -> bool:
    """Convenience wrapper around the default limiter's ``check()``.

    Uses ``get_default_limiter()`` as the single source of truth so that
    ``limiter.reset(name)`` in tests correctly resets the rate-limit
    state.  The previous implementation delegated to a separate
    ``infra.rate_limiter.RATE_LIMITERS`` dict which caused a mismatch.
    """
    return get_default_limiter().check(name)


def reset_rate_limiter() -> None:
    """Reset the default rate limiter (for tests)."""
    global _default_limiter
    with _default_limiter_lock:
        _default_limiter = None


# ---------------------------------------------------------------------------
# deprecated decorator
# ---------------------------------------------------------------------------


def deprecated(message: str = ""):
    """Decorator: mark a function as deprecated with a runtime warning.

    Emits a ``DeprecationWarning`` each call.  For static-analysis
    support (IDE strikethrough), callers should also add a
    ``@typing.deprecated`` (Python 3.13+) or a stub.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            warnings.warn(
                message or f"{func.__name__} is deprecated",
                DeprecationWarning,
                stacklevel=2,
            )
            return func(*args, **kwargs)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Lazy config-attr resolution (P2-1 consolidation)
# ---------------------------------------------------------------------------
#
# Many modules expose a single `*_ENABLED` (or similar) feature flag via
# a module-level __getattr__ that reads from MemoryConfig. The pattern
# was duplicated 14 times. This helper centralises it.
#
# Usage at the bottom of a module:
#
#     from memory_common import make_lazy_getattr
#     __getattr__ = make_lazy_getattr({"X_ENABLED": "x_enabled"})
#
# Or, if the attribute is the negation of a config field:
#
#     __getattr__ = make_lazy_getattr(
#         {"RERANKER_ENABLED": ("reranker_disabled", True)}
#     )
#
# Behaviour matches the inline pattern that was duplicated:
# - Reads `get_config().<attr>` on first access
# - Caches the resolved value in the module's __dict__ so subsequent
#   bare-name references inside module functions resolve normally
#   (Python's name lookup checks __dict__ before triggering __getattr__)
# - Raises AttributeError for unknown attributes

# Each entry in name_to_attr maps a module attribute name to either:
#   - a string (the MemoryConfig field name), or
#   - a 2-tuple (config_field_name, transform) where transform is a
#     callable applied to the config value (e.g. for negation).


def make_lazy_getattr(
    name_to_attr: dict,
    cache: bool = True,
):
    """Return a module-level __getattr__ that lazily resolves config attrs.

    Args:
        name_to_attr: dict mapping module attribute names to either a
            string (config field name) or a 2-tuple
            (config_field_name, transform).
        cache: if True, cache resolved values in the **target module's
            __dict__** on first access. This makes bare-name references
            inside the module resolve to the cached value (since
            Python's name lookup doesn't trigger __getattr__ for
            bare names), and it ensures ``importlib.reload(module)``
            clears the cache (it replaces the module's __dict__).

            The 2026-06-20 fix moved the cache from the caller's globals
            to the target module's __dict__. Storing in the caller's
            globals caused a test-isolation bug: a test that ran first
            and resolved an attr (e.g. ``reranker.RERANKER_ENABLED``)
            would cache the value in the test module's globals, where
            ``importlib.reload(reranker)`` couldn't clear it.

    Returns:
        A function suitable for assignment to a module's __getattr__:

            __getattr__ = make_lazy_getattr({"X_ENABLED": "x"})

    """

    # Resolve the target module once at decoration time. ``__getattr__``
    # is defined at module top level, so its globals() is the target
    # module's namespace.
    _target_globals = globals()

    # Mark this module so reset_all_lazy_config_attrs() can find it
    # without sweeping every module in sys.modules.
    _target_globals["_lazy_config_attr_names"] = frozenset(name_to_attr.keys())

    def __getattr__(name):
        if name in name_to_attr:
            spec = name_to_attr[name]
            from config import get_config

            if isinstance(spec, tuple):
                attr_name, transform = spec
                value = getattr(get_config(), attr_name)
                if callable(transform):
                    value = transform(value)
            else:
                value = getattr(get_config(), spec)

            if cache:
                # Store in the target module's __dict__ (which is this
                # function's globals). importlib.reload(target_module)
                # replaces the module's __dict__, which clears the cache.
                _target_globals[name] = value
            return value
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    return __getattr__


def reset_all_lazy_config_attrs() -> None:
    """Clear the lazy-config-attr cache in every module that uses one.

    Only touches modules that carry the ``_lazy_config_attr_names``
    marker set by :func:`make_lazy_getattr` (or added manually for
    hand-rolled ``__getattr__`` sites like ``search_pipeline``).
    All other modules — including test modules that import a lazy
    module as a local name — are left untouched.
    """
    import sys

    for _mod_name, mod in list(sys.modules.items()):
        cached_names = mod.__dict__.get("_lazy_config_attr_names")
        if not cached_names:
            continue
        for attr in cached_names:
            mod.__dict__.pop(attr, None)


def _resolve_tags(
    category: str,
    caller_tags: list[str] | str | None,
    *,
    context: str = "generic",
    tool_slug: str = "",
) -> list[str]:
    """Centralise all tag-policy decisions so callers cannot diverge.

    Policy
    ------
    1. Caller-supplied tags always win — never overwrite.
    2. Auto-save hook: always prepend [auto-save, hook, tool-log, <slug>].
    3. MCP memory_save: when caller passes nothing and category is
       lessons or decisions, default to [category].
    4. All inputs are normalised (None → [], str → split, list → strip).
    """
    if caller_tags is None:
        base: list[str] = []
    elif isinstance(caller_tags, str):
        try:
            base = json.loads(caller_tags)
        except json.JSONDecodeError:
            base = [t.strip() for t in re.split(r"[,; ]+", caller_tags) if t.strip()]
    elif isinstance(caller_tags, list):
        base = [str(t).strip() for t in caller_tags if t]
    else:
        base = []

    if context == "auto-save":
        slug = tool_slug.strip() if tool_slug else "unknown"
        return ["auto-save", "hook", "tool-log", slug] + base
    if context == "mcp" and not base and category in ("lessons", "decisions"):
        return [category]
    return base


_STATE_DIR = Path.home() / ".config" / "agentic-memory"


def _compliance_last_warn_path() -> Path:
    return _STATE_DIR / "compliance_last_warn.json"


def should_complain_about_score(
    score: float, *, window_seconds: float = 86400.0, change_threshold: float = 10.0
) -> bool:
    state_path = _compliance_last_warn_path()
    now = time.time()
    try:
        raw = state_path.read_text(encoding="utf-8")
        prev = json.loads(raw)
    except (OSError, json.JSONDecodeError, TypeError):
        prev = {}
    prev_score = prev.get("score")
    prev_ts = prev.get("ts", 0)
    if prev_score is None:
        state_path.write_text(
            json.dumps({"score": round(score, 2), "ts": round(now)}),
            encoding="utf-8",
        )
        return True
    if abs(float(prev_score) - score) >= change_threshold:
        state_path.write_text(
            json.dumps({"score": round(score, 2), "ts": round(now)}),
            encoding="utf-8",
        )
        return True
    if (now - float(prev_ts)) >= window_seconds:
        state_path.write_text(
            json.dumps({"score": round(score, 2), "ts": round(now)}),
            encoding="utf-8",
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Session lifecycle helpers (Phase D)
# ---------------------------------------------------------------------------

def get_sessions_dir() -> Path:
    """Return the active memory dir's sessions directory."""
    try:
        _, local_mem, _ = get_memory_paths()
        return local_mem / "sessions"
    except Exception:
        return Path.home() / ".config" / "agentic-memory" / "memory" / "sessions"


_CURRENT_SESSION_FILE = get_sessions_dir() / ".current_session.json"


def read_current_session() -> dict:
    if not _CURRENT_SESSION_FILE.exists():
        return {}
    try:
        return cast(dict, json.loads(_CURRENT_SESSION_FILE.read_text()))
    except (json.JSONDecodeError, OSError):
        return {}


def is_session_active(max_age_seconds: float = 3600.0) -> bool:
    """Return True if a session was started within max_age_seconds."""
    data = read_current_session()
    started_at = data.get("started_at")
    if not started_at:
        return False
    try:
        elapsed = time.time() - float(started_at)
        return elapsed < max_age_seconds
    except (TypeError, ValueError):
        return False


def ensure_session_active(max_age_seconds: float = 3600.0) -> bool:
    """Ensure a session is active; return True if one already was active.

    This does NOT produce output — it only writes/refreshes the
    .current_session.json state so that subsequent memory_search and
    save_memory calls know a session exists.
    """
    if is_session_active(max_age_seconds):
        return True
    sessions_dir = get_sessions_dir()
    sessions_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "started_at": time.time(),
        "started_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "ensure_session_active",
    }
    try:
        _CURRENT_SESSION_FILE.write_text(json.dumps(data, indent=2))
    except OSError:
        pass
    return False
