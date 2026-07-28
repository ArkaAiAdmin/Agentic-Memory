"""Memory-system paths and configuration constants.

Extracted from memory_common.py during the 6-module refactor.

Provides:
  * ``GLOBAL_MEM_DIR``: directory containing the actual global notes.
  * ``get_memory_paths``: resolve (project_root, local_mem, global_mem) for cwd.
  * ``find_project_root``: traverse upwards looking for ``PROJECT_ROOT_MARKERS``.
  * ``PROJECT_ROOT_MARKERS``: the markers ``find_project_root`` accepts.
  * ``configure_logging``: idempotent root-logging setup (text or JSON).
  * ``log_backup``: copy ``db_path`` to a timestamped ``.db.bak.YYYYMMDD-HHMMSS``.
  * ``validate_config``: validate env vars; returns list of warning strings.

The constants module is named ``memory_config`` (not ``config``) so it does
not collide with the existing ``config.py`` (which exposes ``MemoryConfig``).
"""

from __future__ import annotations

import datetime
import logging
import os
import shutil
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

PROJECT_ROOT_MARKERS = (
    "memory",
    ".git",
    ".agents",
    "AGENTS.md",
    "CLAUDE.md",
    "package.json",
    "pyproject.toml",
)

GLOBAL_MEM_DIR = Path(
    os.environ.get(
        "GLOBAL_MEM_DIR",
        Path.home() / ".config" / "agentic-memory" / "memory",
    )
)


def install_root() -> Path:
    """Resolve the agentic-memory install root (where the package lives).

    Order of resolution:
    1. ``MEMORY_INSTALL_ROOT`` env var (explicit override for non-default installs)
    2. ``~/.config/agentic-memory/`` (the default install location)

    This is the single source of truth for "where is the install", so that
    hooks and eval tests don't have to hard-code the path. Use this from
    any site that needs to add the install dir to ``sys.path``.
    """
    env_root = os.environ.get("MEMORY_INSTALL_ROOT")
    if env_root:
        return Path(env_root).expanduser()
    return Path.home() / ".config" / "agentic-memory"


def venv_python_path() -> Path | None:
    """Resolve the venv python executable that lives next to the install root.

    Returns ``<install_root>/venv/bin/python`` if it exists, else ``None``.

    Useful as a fallback for subprocess calls: prefer the venv python over
    whatever ``sys.executable`` happens to be, so that operations run
    inside the same Python environment the package was installed into.
    """
    candidate = install_root() / "venv" / "bin" / "python"
    return candidate if candidate.exists() else None


_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

__all__ = [
    "GLOBAL_MEM_DIR",
    "get_memory_paths",
    "find_project_root",
    "configure_logging",
    "log_backup",
    "validate_config",
    "install_root",
    "venv_python_path",
]


def find_project_root(start_path):
    """Traverse upwards to find the project root.

    Recognises the same set of markers as the MCP server's previous in-file
    copy (H1-b fix: the two implementations were drifting). New marker types
    can be added to PROJECT_ROOT_MARKERS without touching callers.
    """
    if isinstance(start_path, str):
        start_path = Path(start_path)
    for path in [start_path] + list(start_path.parents):
        for marker in PROJECT_ROOT_MARKERS:
            if (path / marker).exists():
                return path
    return None


def get_memory_paths() -> "Tuple[Path, Path, Path]":
    """Resolve the (project_root, local_mem, global_mem) triple for the current cwd.

    If no project marker is found, returns (start_path, start_path/'memory',
    global). Callers that perform destructive writes (memory_save,
    memory_compact) should treat the lack of an existing local `memory/` as
    an error rather than silently creating one.

    `global_mem` is the directory containing the actual global notes
    (decisions/, lessons/, ...). The scripts (.py) that operate on it live
    at `GLOBAL_SCRIPTS_DIR` (one level up).

    DB location heuristic:
    1. If MEMORY_DB_PATH is set in environment → local_mem = db_path.parent
    2. If project_root/memory/memory.db exists → local_mem = project_root/memory
    3. If project_root/memory.db exists directly → local_mem = project_root
    4. Otherwise → local_mem = project_root/memory (may not exist yet)
    """
    env_db = os.environ.get("MEMORY_DB_PATH")
    if env_db:
        db_path = Path(env_db)
        local_mem = db_path.parent
        project_root = find_project_root(local_mem) or local_mem
        return (project_root, local_mem, GLOBAL_MEM_DIR)

    cwd = Path(os.getcwd())
    project_root = find_project_root(cwd)
    if project_root is None:
        project_root = cwd
    # Prefer memory/ subdir if it contains a DB, else check project root directly
    mem_subdir = project_root / "memory"
    if (mem_subdir / "memory.db").exists():
        local_mem = mem_subdir
    elif (project_root / "memory.db").exists():
        local_mem = project_root
    elif (GLOBAL_MEM_DIR / "memory.db").exists():
        # Fallback: use global memory dir if local doesn't have a DB
        local_mem = GLOBAL_MEM_DIR
    else:
        local_mem = mem_subdir
    return (project_root, local_mem, GLOBAL_MEM_DIR)


def configure_logging() -> None:
    """Configure root logging once. Idempotent and lazy.

    H1 fix: replaces the 103 `print()` calls across the 7 main files with
    a single shared `logging.basicConfig`. Safe to call from any entry
    point (memory_mcp, e2e_full_pass, eval/deep_e2e). Subsequent calls
    are no-ops, so importing this module from a test that already
    configured logging does not clobber the test's handler.

    Reads `LOG_LEVEL` from the environment (default `INFO`).
    Set `LOG_FORMAT=json` for structured JSON output (useful for production).
    """
    import json as _json_mod

    if logging.getLogger().handlers:
        return
    log_format = os.environ.get("LOG_FORMAT", "text")
    if log_format == "json":

        class _JsonFormatter(logging.Formatter):
            def format(self, record):
                log_entry = {
                    "ts": self.formatTime(record, self.datefmt),
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": record.getMessage(),
                }
                if record.exc_info and record.exc_info[0]:
                    log_entry["exception"] = self.formatException(record.exc_info)
                return _json_mod.dumps(log_entry, default=str)

        handler = logging.StreamHandler()
        handler.setFormatter(_JsonFormatter())
        logging.root.addHandler(handler)
        logging.root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    else:
        logging.basicConfig(
            level=os.environ.get("LOG_LEVEL", "INFO"),
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        )
    validate_config()


def log_backup(db_path: Path, keep: int = 3) -> None:
    """Copy `db_path` to a timestamped `.db.bak.YYYYMMDD-HHMMSS` sibling.

    H2 fix: before any rebuild that does `os.replace(tmp, db_path)`, copy
    the current `db_path` aside so a semantically-bad rebuild can be
    rolled back by hand. Preserves mtime via `shutil.copy2`.

    Only the `keep` most recent backups (default 3) are retained; older
    ones are deleted. Best-effort — logs and continues on error.
    """

    db_path = Path(db_path)
    if not db_path.exists():
        return
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = db_path.with_suffix(f".db.bak.{ts}")
    try:
        shutil.copy2(str(db_path), str(backup_path))
        logger.info("Created DB backup: %s", backup_path)
    except Exception as e:
        logger.warning("Failed to create DB backup of %s: %s", db_path, e)
        return
    try:
        db_path.name + ".bak."
        backups = sorted(
            db_path.parent.glob(db_path.name + ".bak.*"),
            key=lambda p: p.name,
            reverse=True,
        )
        for old in backups[keep:]:
            try:
                old.unlink()
                logger.info("Pruned old backup: %s", old)
            except Exception as e:
                logger.warning("Failed to prune backup %s: %s", old, e)
    except Exception as e:
        logger.warning("Backup rotation failed: %s", e)


def validate_config() -> list[str]:
    """Validate environment variables at startup. Returns list of warnings.

    Call once at import time or from configure_logging(). Does not raise —
    logs warnings for misconfigured values and falls back to defaults.
    """
    warnings = []
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    if log_level.upper() not in _VALID_LOG_LEVELS:
        warnings.append(
            f"LOG_LEVEL={log_level!r} not in {sorted(_VALID_LOG_LEVELS)}, using INFO"
        )
        os.environ["LOG_LEVEL"] = "INFO"
    for key, default, validator in [
        ("MEMORY_FTS5_CACHE_TTL", "300", lambda v: int(v) > 0),
        ("MEMORY_FTS5_CACHE", "1", lambda v: v in ("0", "1")),
        ("MEMORY_WAL_CHECKPOINT_STARTUP", "1", lambda v: v in ("0", "1")),
        ("MEMORY_UNINDEXED_SAFETY_NET_LIMIT", "1000", lambda v: int(v) > 0),
    ]:
        raw = os.environ.get(key, default)
        try:
            if not validator(raw):
                warnings.append(f"{key}={raw!r} invalid, using default {default}")
                os.environ[key] = default
        except (ValueError, TypeError):
            warnings.append(f"{key}={raw!r} invalid, using default {default}")
            os.environ[key] = default
    if warnings:
        import logging

        logging.getLogger(__name__).warning(
            "Config validation: %s", "; ".join(warnings)
        )
    return warnings
