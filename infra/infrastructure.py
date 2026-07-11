#!/usr/bin/env python3
from __future__ import annotations
"""Infrastructure utilities for the memory system.

Shared helpers used across save, search, and tool layers:
- DB path resolution (active dir, per-memory-id lookup)
- Audit decorator + rate limiting
- Error envelope
- Unicode normalization
- Markdown index helpers
"""

__all__ = [
    "resolve_active_memory_dir",
    "resolve_db_for_memory_id",
    "with_audit",
    "with_memory_connection",
    "ErrorCode",
    "classify_exception",
    "add_link_to_memory_md_content",
    "update_memory_md_locked",
    "GLOBAL_MEM_DIR",
]
import json
import logging
import os
import unicodedata
import functools
from enum import Enum
from pathlib import Path

from infra.memory_common import (
    atomic_write,
    configure_logging,
    safe_close_db,
    connection_pool,
    acquire_flock_with_retry,
    release_flock,
    rate_limit_check,  # noqa: F401 — re-exported; patched by tests at infra.infrastructure.rate_limit_check
    get_memory_paths,
    GLOBAL_MEM_DIR,
)

configure_logging()
logger = logging.getLogger(__name__)

import infra.audit as audit  # noqa: E402


# ---------------------------------------------------------------------------
# Unicode normalization
# ---------------------------------------------------------------------------


def _normalize_unicode(text: str) -> str:
    """NFKC normalize a string for FTS5 indexing. Idempotent."""
    if text is None:
        return text
    return unicodedata.normalize("NFKC", text)


# ---------------------------------------------------------------------------
# DB path resolution
# ---------------------------------------------------------------------------


def _resolve_active_db_path(base_dir: Path | None = None) -> str:
    """Absolute path to the active memory DB for audit routing.

    Falls back to the global prod DB if resolution fails. Audit
    must never break a tool call.

    Args:
        base_dir: optional explicit memory directory. When provided,
                  bypasses CWD-based path resolution (see resolve_active_memory_dir).
    """
    env_path = os.environ.get("MEMORY_DB_PATH")
    if env_path is not None:
        return env_path
    try:
        return str(resolve_active_memory_dir(base_dir=base_dir) / "memory.db")
    except Exception as e:
        logger.debug("audit: could not resolve active DB, using global: %s", e)
        return str(GLOBAL_MEM_DIR / "memory.db")


def resolve_active_memory_dir(base_dir: Path | None = None) -> Path:
    """Return the memory dir that actually has a DB to operate on.

    Resolution order:
    1. ``base_dir`` if explicitly provided.
    2. ``MEMORY_DB_PATH`` env var if set.
    3. Local project memory dir if it contains a ``memory.db`` file.
    4. Global memory dir.

    The previous row-count heuristic (pick the DB with the most rows) is
    removed because it caused a chicken-and-egg problem: a fresh local DB
    could never win against an established global DB, silently routing all
    writes to the global folder and breaking repository-scoping.

    Args:
        base_dir: optional explicit memory directory. When provided,
                  skips CWD-based ``get_memory_paths()`` resolution and
                  uses this directory directly. Useful for scripts that
                  know the DB location independently of CWD.
    """
    if base_dir is not None:
        return base_dir

    env_path = os.environ.get("MEMORY_DB_PATH")
    if env_path is not None:
        return Path(env_path).parent

    _, local_mem, global_mem = get_memory_paths()

    # Local DB exists → use it (even if empty). This is the local-first
    # contract: each project owns its own memory folder.
    if (local_mem / "memory.db").exists():
        return local_mem  # type: ignore[no-any-return]

    return global_mem  # type: ignore[no-any-return]


def resolve_db_for_memory_id(
    memory_id: str, base_dirs: tuple[Path, ...] | None = None
) -> Path | None:
    """Find the DB that contains a given memory_id.

    Used by write-side tools (memory_reinforce) that need to update a
    specific note — the note may live in local OR global memory, and we
    must update the row in the right place.

    Args:
        memory_id: the note ID to search for.
        base_dirs: optional explicit directories to search. When provided,
                   bypasses CWD-based ``get_memory_paths()`` resolution.

    Returns None if the memory_id is not present in any DB.
    """
    if base_dirs is not None:
        dirs_to_check = base_dirs
    else:
        _, local_mem, global_mem = get_memory_paths()
        dirs_to_check = (local_mem, global_mem)
    for base in dirs_to_check:
        db = base / "memory.db"
        if not db.exists():
            continue
        conn = None
        try:
            conn = connection_pool.get(str(db), timeout=5.0)
            row = conn.execute(
                "SELECT 1 FROM memories WHERE id=? LIMIT 1", (memory_id,)
            ).fetchone()
            if row:
                return db
        except Exception:
            logger.warning("Failed to probe memory %s in database %s", memory_id, db)
            continue
        finally:
            if conn is not None:
                safe_close_db(conn)
    return None


# ---------------------------------------------------------------------------
# Audit decorator + rate limiting
# ---------------------------------------------------------------------------


def _try_extract_result_meta(result: str, ctx: dict) -> None:
    """Best-effort: populate ``ctx['results_count']`` and ``ctx['top1_id']``."""
    if not isinstance(result, str):
        return
    try:
        r = json.loads(result)
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(r, dict):
        return
    if "results" in r and isinstance(r["results"], list):
        ctx["results_count"] = len(r["results"])
        if r["results"] and isinstance(r["results"][0], dict):
            tid = r["results"][0].get("id")
            if isinstance(tid, str):
                ctx["top1_id"] = tid
        return
    if "count" in r and isinstance(r["count"], int):
        ctx["results_count"] = r["count"]


def with_audit(tool_name: str):
    """Decorator: wrap an MCP tool with audit logging + rate limiting.

    Must be applied as the innermost wrapper (closer to the function
    than ``@mcp.tool()``) so the registered tool name is preserved.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            from infra.memory_common import rate_limit_check as real_rl_check
            if not real_rl_check(tool_name):
                from infra.rate_limiter import get_retry_after
                retry_after = get_retry_after(tool_name)
                return _err(
                    ErrorCode.RATE_LIMITED,
                    f"Too many calls to {tool_name}. Try again later. (retry_after={retry_after:.1f}s)",
                )
            db_path = _resolve_active_db_path()
            audit_args = dict(kwargs) if kwargs else {}
            audit_args.setdefault("tenant_id", "default")
            with audit.audit(
                tool_name,
                args=audit_args,
                db_path=db_path,
            ) as ctx:
                result = func(*args, **kwargs)
                _try_extract_result_meta(result, ctx)
                return result

        return wrapper

    return decorator


def with_memory_connection(func):
    """Decorator: wrap an MCP tool with connection lifecycle management.

    The decorated function receives ``conn`` (sqlite3.Connection) as its
    first positional argument. The connection is obtained from
    ``connection_pool.get()``, configured with ``PRAGMA busy_timeout = 30000``,
    and automatically closed on return.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        db_path = _resolve_active_db_path()
        tenant_id = kwargs.get("tenant_id", "default")
        conn = connection_pool.get(str(db_path), timeout=30.0, tenant_id=tenant_id)
        try:
            conn.execute("PRAGMA busy_timeout = 30000;")
            return func(conn, *args, **kwargs)
        finally:
            safe_close_db(conn)

    return wrapper


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


class ErrorCode(Enum):
    DB_ERROR = "DB_ERROR"
    DB_LOCK = "DB_LOCK"
    DB_TIMEOUT = "DB_TIMEOUT"
    DB_CONNECTION = "DB_CONNECTION"
    NOT_FOUND = "NOT_FOUND"
    INVALID_PARAMS = "INVALID_PARAMS"
    TIMEOUT = "TIMEOUT"
    SCHEMA_MISSING = "SCHEMA_MISSING"
    QUALITY_ERROR = "QUALITY_ERROR"
    SUMMARY_ERROR = "SUMMARY_ERROR"
    PROFILE_ERROR = "PROFILE_ERROR"
    RETENTION_ERROR = "RETENTION_ERROR"
    SHARE_ERROR = "SHARE_ERROR"
    RECALL_ERROR = "RECALL_ERROR"
    SESSION_START_ERROR = "SESSION_START_ERROR"
    CTR_FEEDBACK_ERROR = "CTR_FEEDBACK_ERROR"
    CONCEPT_DRIFT_ERROR = "CONCEPT_DRIFT_ERROR"
    CONTENT_TOO_LARGE = "CONTENT_TOO_LARGE"
    INVALID_CATEGORY = "INVALID_CATEGORY"
    INVALID_SLUG = "INVALID_SLUG"
    RATE_LIMITED = "RATE_LIMITED"
    TRAVERSAL = "TRAVERSAL"
    EXPORT_ERROR = "EXPORT_ERROR"
    IMPORT_ERROR = "IMPORT_ERROR"
    INJECTION_DETECTED = "INJECTION_DETECTED"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"


_HINT_BY_CODE: dict[ErrorCode, tuple[str, str]] = {
    ErrorCode.DB_LOCK: (
        "Database is locked by another writer (e.g. compaction). Wait a few seconds and retry.",
        "retry",
    ),
    ErrorCode.DB_TIMEOUT: (
        "DB write timed out. Check for contention or retry with a longer timeout.",
        "retry",
    ),
    ErrorCode.DB_CONNECTION: (
        "Could not connect to the memory DB. Verify the store is accessible.",
        "wait",
    ),
    ErrorCode.TIMEOUT: (
        "Operation timed out. Check system load or retry.",
        "retry",
    ),
    ErrorCode.RATE_LIMITED: (
        "Rate limited. Wait before retrying.",
        "retry",
    ),
    ErrorCode.SCHEMA_MISSING: (
        "Schema is missing or corrupt. Run memory_maintenance(operation=\"check_integrity\").",
        "report",
    ),
    ErrorCode.CONTENT_TOO_LARGE: (
        "Content exceeds the maximum allowed size. Reduce content length.",
        "abort",
    ),
    ErrorCode.INJECTION_DETECTED: (
        "Potential injection detected in input. Review the content.",
        "abort",
    ),
    ErrorCode.AUTHORIZATION_DENIED: (
        "You do not have permission for this operation. Check your role bindings.",
        "abort",
    ),
}


def _err(
    code: ErrorCode,
    message: str,
    hint: str | None = None,
    action: str | None = None,
) -> str:
    """Return a structured error envelope with recovery hint.

    Format::

        Error [CODE]: message
        Hint: <recovery hint>
        Action: <retry|wait|abort|report>

    The ``Hint`` and ``Action`` lines are suppressed when the hint is not
    provided and the code has no default hint or an overridden action.
    """
    lines: list[str] = [f"Error [{code.value}]: {message}"]
    effective_hint = hint or (_HINT_BY_CODE.get(code, ("", ""))[0] if code in _HINT_BY_CODE else "")
    effective_action = action or (_HINT_BY_CODE.get(code, ("", ""))[1] if code in _HINT_BY_CODE else "")
    if effective_hint:
        lines.append(f"Hint: {effective_hint}")
    if effective_action:
        lines.append(f"Action: {effective_action}")
    return "\n".join(lines)


_LOCK_KEYWORDS = (
    "database is locked",
    "database table is locked",
    "database disk image is malformed",
)
_TIMEOUT_KEYWORDS = ("timeout", "timed out", "deadline")
_CONNECTION_KEYWORDS = (
    "unable to open database file",
    "disk i/o error",
    "access denied",
    "connection",
)


def classify_exception(e: Exception) -> ErrorCode:
    """Best-effort classification of common memory/DB errors.

    Falls back to ``ErrorCode.DB_ERROR`` for unrecognized exceptions.
    """
    msg = str(e).lower()
    if any(kw in msg for kw in _LOCK_KEYWORDS):
        return ErrorCode.DB_LOCK
    if any(kw in msg for kw in _TIMEOUT_KEYWORDS):
        return ErrorCode.DB_TIMEOUT
    if any(kw in msg for kw in _CONNECTION_KEYWORDS):
        return ErrorCode.DB_CONNECTION
    return ErrorCode.DB_ERROR


# ---------------------------------------------------------------------------
# Markdown index helpers
# ---------------------------------------------------------------------------


def add_link_to_memory_md_content(content: str, category: str, title_slug: str) -> str:
    target_link = f"[[{category}/{title_slug}.md]]"
    if target_link in content:
        return content

    lines = content.splitlines()
    category_keywords = {
        "projects": ["active projects", "projects"],
        "decisions": ["architecture decisions", "adr", "decisions"],
        "lessons": ["hard-won lessons", "lessons", "learnings"],
        "preferences": ["user preferences", "preferences", "settings"],
        "quirks": ["quirks", "known issues", "quirks & known issues"],
        "sessions": ["session logs", "sessions", "session log"],
        "docs": ["documentation", "docs"],
    }
    keywords = category_keywords.get(category.lower(), [category.lower()])

    header_idx = -1
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            header_text = stripped.lstrip("#").strip().lower()
            if any(kw in header_text for kw in keywords):
                header_idx = idx
                break

    rel_link = f"- [[{category}/{title_slug}.md]] — {title_slug.replace('-', ' ').title()} context."
    if header_idx != -1:
        lines.insert(header_idx + 1, rel_link)
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"## {category.title()}")
        lines.append(rel_link)

    return "\n".join(lines) + "\n"


def update_memory_md_locked(index_file_path: Path, category: str, title_slug: str):
    """Atomically update MEMORY.md to include a new link.

    Reads under an exclusive flock, then writes to a sibling temp file and
    os.replace()s into place.
    """
    from infra._lazy_imports import FileLockError

    lock_dir = index_file_path.parent
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".memory_md.lock"
    lock_file = open(lock_path, "w")
    # P1-15 fix: this is a write-path caller; a held lock means a
    # concurrent update is in flight, which would race the os.replace
    # below. Use ``strict=True`` so we surface the conflict as a
    # logged FileLockError rather than silently dropping the lock.
    try:
        acquire_flock_with_retry(
            lock_file, max_attempts=5, initial_backoff=0.05, strict=True
        )
    except FileLockError as e:
        try:
            lock_file.close()
        except Exception:
            logger.warning("Failed to close lock file for MEMORY.md update")
            pass
        logger.error(
            "Could not acquire lock for MEMORY.md update: %s: %s", lock_path, e
        )
        return
    try:
        if index_file_path.exists():
            content = index_file_path.read_text(encoding="utf-8")
        else:
            content = (
                "# Agentic Memory Index\n\n"
                "## Active Projects\n\n"
                "## Architecture Decisions (ADRs)\n\n"
                "## Hard-Won Lessons\n\n"
                "## User Preferences\n"
            )
        new_content = add_link_to_memory_md_content(content, category, title_slug)
        if new_content != content:
            atomic_write(index_file_path, new_content, encoding="utf-8")
    finally:
        release_flock(lock_file)
