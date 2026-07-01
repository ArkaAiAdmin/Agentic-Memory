"""
Rebuild subsystem MCP tools — rebuild, backfill_all, compact.

Extracted from mcp_maintenance.py to reduce module size.
"""
from mcp_common import _bootstrap_path  # noqa: E402,F401

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from infra.cache import clear_all_caches
from mcp_common import (
    _resolve_memory_dir,
    _err,
    ErrorCode,
    GLOBAL_SCRIPTS_DIR,
    GLOBAL_MEM_DIR,
    get_memory_paths,
    logger,
    _run_subprocess_output,
    with_audit,
)
from mcp_instance import mcp


@mcp.tool()
@with_audit("memory_rebuild")
def memory_rebuild(scope: str = "active") -> str:
    """Rebuild the SQLite FTS5 (full-text search) index from the source markdown files.

    USE THIS TOOL WHEN:
    - You suspect the SQLite search index is desynchronized, corrupted, or missing.
    - You manually edited the source markdown files on the local filesystem and want to sync the SQLite index.

    ARGUMENTS:
    - scope: The target scope for the rebuild. Choose from:
        * 'active': The active memory directory (default).
        * 'local': The workspace-specific local memory.
        * 'global': The global user memory configuration directory.

    RETURNS:
    A status string indicating whether the rebuild succeeded.
    """
    active_dir = _resolve_memory_dir()
    if os.environ.get("MEMORY_DB_PATH"):
        local_mem = active_dir
        global_mem = Path(GLOBAL_MEM_DIR)
    else:
        cwd, local_mem, global_mem = get_memory_paths()
    rebuild_script = GLOBAL_SCRIPTS_DIR / "rebuild_index.py"
    if not rebuild_script.exists():
        return _err(
            ErrorCode.NOT_FOUND, f"Global rebuild script not found at {rebuild_script}"
        )

    if scope == "global":
        source_dir = global_mem
        db_path = global_mem / "memory.db"
    elif scope == "local":
        source_dir = local_mem
        db_path = local_mem / "memory.db"
    elif scope == "active":
        active = _resolve_memory_dir()
        source_dir = active
        db_path = active / "memory.db"
    else:
        return _err(
            ErrorCode.INVALID_PARAMS,
            f"scope must be 'active', 'local', or 'global' (got {scope!r}).",
        )

    try:
        result = subprocess.run(
            [sys.executable, str(rebuild_script), str(source_dir), str(db_path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout
        if result.stderr:
            output += "\n" + result.stderr
        if result.returncode != 0:
            return f"Rebuild script exited {result.returncode}:\n{output}"
        clear_all_caches()
        return (
            f"Memory index rebuilt successfully ({scope} scope).\n{output}"
            if output
            else f"Memory index rebuilt successfully ({scope} scope)."
        )
    except subprocess.TimeoutExpired:
        logger.exception("rebuild timed out after 120s")
        return _err(ErrorCode.TIMEOUT, "rebuild timed out after 120s")
    except Exception:
        logger.exception("Failed to rebuild index")
        return _err(ErrorCode.DB_ERROR, "Failed to rebuild index")


@mcp.tool()
@with_audit("memory_compact")
def memory_compact(dry_run: bool = False) -> str:
    """Run tier migration + consolidation + rebuild + session archival."""
    results = []
    active = _resolve_memory_dir()
    tier_script = GLOBAL_SCRIPTS_DIR / "tier_migration.py"
    if tier_script.exists():
        cmd = [sys.executable, str(tier_script)]
        if dry_run:
            cmd.append("--dry-run")
        out, _ = _run_subprocess_output(cmd, timeout=60, cwd=str(active))
        results.append(f"Tier Migration (dry_run={dry_run}):\n{out}")

    consolidate_script = GLOBAL_SCRIPTS_DIR / "consolidate_facts.py"
    if consolidate_script.exists():
        out, _ = _run_subprocess_output(
            [sys.executable, str(consolidate_script)], timeout=120, cwd=str(active)
        )
        results.append(f"Fact Consolidation:\n{out[:500]}")

    rebuild_script = GLOBAL_SCRIPTS_DIR / "rebuild_index.py"
    if rebuild_script.exists():
        out, _ = _run_subprocess_output(
            [
                sys.executable,
                str(rebuild_script),
                str(active),
                str(active / "memory.db"),
            ],
            timeout=60,
        )
        results.append(f"Index Rebuild:\n{out}")

    sessions_dir = active / "sessions"
    archive_dir = active / "archive" / "sessions"
    if sessions_dir.exists():
        if not dry_run:
            archive_dir.mkdir(parents=True, exist_ok=True)
            for f in sessions_dir.glob("*.md"):
                if f.stat().st_mtime < (time.time() - 14 * 86400):
                    src = Path(f)
                    dst = archive_dir / f.name
                    try:
                        if src.stat().st_dev == dst.parent.stat().st_dev:
                            os.replace(str(src), str(dst))
                        else:
                            logger.warning(
                                "Cross-device move for %s; falling back to shutil.move.",
                                src,
                            )
                            shutil.move(str(src), str(dst))
                    except OSError as e:
                        logger.error("Failed to archive session %s: %s", src, e)
                        raise
                    results.append(f"Archived session: {f.name}")
        else:
            count = sum(
                1
                for f in sessions_dir.glob("*.md")
                if f.stat().st_mtime < (time.time() - 14 * 86400)
            )
            if count:
                results.append(
                    f"[DRY RUN] Would archive {count} sessions older than 14 days."
                )

    try:
        db_path = active / "memory.db"
        if db_path.exists():
            from infra.memory_common import wal_checkpoint_idle

            ckpt = wal_checkpoint_idle(db_path, wal_size_threshold_mb=1.0)
            if ckpt.get("status") != "skipped":
                results.append(f"WAL Checkpoint:\n{json.dumps(ckpt, indent=2)}")
    except Exception as e:
        results.append(f"WAL Checkpoint (error, non-fatal): {e}")

    return "\n\n".join(results)


@mcp.tool()
@with_audit("memory_backfill_all")
def memory_backfill_all(mode: str = "health", source: str = "") -> str:
    """Universal memory index backfill orchestrator.

    Unified entry point for rebuilding all memory indexes: FTS5, embeddings,
    chunks, KG facts, KG graph (entities+edges), vector index, and backlinks.
    """
    from pathlib import Path as _Path
    from backfill.orchestrator import (
        health_check,
        backfill_incremental,
        backfill_full,
        auto_backfill,
    )

    db_path = _resolve_memory_dir() / "memory.db"
    if not db_path.exists():
        return _err(ErrorCode.DB_ERROR, f"No memory.db at {db_path}")

    src = _Path(source) if source else _resolve_memory_dir()
    try:
        result: dict[str, Any]
        if mode == "health":
            result = health_check(db_path)
        elif mode == "incremental":
            result = backfill_incremental(db_path, src)
        elif mode == "full":
            result = backfill_full(db_path, src)
        elif mode == "auto":
            ab = auto_backfill(db_path)
            result = (
                ab
                if ab is not None
                else {"result": "skipped", "reason": "interval not reached"}
            )
            if result is None:
                return json.dumps(
                    {"result": "skipped", "reason": "interval not reached"}
                )
        else:
            return _err(
                ErrorCode.INVALID_PARAMS,
                f"Unknown mode '{mode}'. Use: health, incremental, full, auto",
            )
        return json.dumps(result, indent=2)
    except Exception as e:
        return _err(ErrorCode.DB_ERROR, str(e))
