"""
Memory CRUD MCP tools — save, superseede, delete, restore, trash, purge, auto_save*, daily_digest, reinforce.
"""

import _bootstrap_path  # noqa: E402
import os
import sys
from pathlib import Path


import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from mcp_common import (
    _resolve_memory_dir,
    _run_subprocess_output,
    _search_cache,
    GLOBAL_SCRIPTS_DIR,
    GLOBAL_MEM_DIR,
    get_memory_paths,
    logger,
    _err,
    ErrorCode,
    with_audit,
    atomic_write,
    parse_frontmatter,
    _validate_slug,
    resolve_db_for_memory_id,
)
from mcp_instance import mcp
from save_pipeline import save_memory


@mcp.tool()
@with_audit("memory_save")
def memory_save(
    content: str,
    category: str,
    title_slug: str,
    tags: Optional[list] = None,
    pinned: bool = False,
    is_global: bool = False,
    importance: int = 3,
) -> str:
    """Write a memory note to disk and update the FTS5 index incrementally. category is a subdirectory under memory/ (lessons, projects, decisions, preferences, sessions). is_global=True routes to ~/.config/agentic-memory/memory/.

    importance: 1-5, default 3. Higher importance boosts the note in
    recall ranking and the spaced-repetition review schedule. Values
    are clamped to [1, 5] by save_pipeline.

    MCP tool wrapper around ``save_memory``. The ``safety_wiring`` kwarg
    is intentionally NOT exposed on this MCP tool — only direct Python
    callers can opt in to the contradiction-checked save. Existing MCP
    callers see the same success / error envelope as before, bit-for-bit.
    """
    if len(content) > 100_000:
        return f"Error: content exceeds 100,000 character limit ({len(content)} chars)"

    result = save_memory(
        content=content,
        category=category,
        title_slug=title_slug,
        tags=tags,
        pinned=pinned,
        is_global=is_global,
        safety_wiring=True,
        importance=importance,
        context="mcp",
        note_id="",
    )
    if isinstance(result, str) and result.startswith("Error "):
        return result
    # B2 fix (2026-06-22): KG indexing is now invoked by
    # ``save_pipeline._update_memory_index_incremental`` via
    # ``_index_kg`` (see save/indexers.py). The previous inline call
    # here bypassed the saga, double-wrote KG facts when CRDT was
    # active, and held a separate DB connection outside the save
    # transaction. Now KG facts are written atomically with the rest
    # of the save, and the indexer is the single source of truth.
    prefix = "global" if is_global else "memory"
    return f"Successfully saved memory: {prefix}/{category}/{title_slug}.md (Index updated incrementally)."


@mcp.tool()
@with_audit("memory_supersede")
def memory_supersede(old_id: str, new_id: str, valid_to: Optional[str] = None) -> str:
    """Mark an existing memory note as superseded by a newer one.

    Sets the old note's `valid_to` to the current time (or a provided ISO
    timestamp) and its `superseded_by` to the new note's id. After this call,
    the old note is excluded from `memory_search` results by default
    (include_invalid=False). The new note is unaffected.
    """
    active_dir = _resolve_memory_dir()
    if os.environ.get("MEMORY_DB_PATH"):
        local_mem = active_dir
        global_mem = Path(GLOBAL_MEM_DIR)
    else:
        _, local_mem, global_mem = get_memory_paths()
    target_base = active_dir
    db_path = target_base / "memory.db"
    if not db_path.exists():
        return _err(
            ErrorCode.DB_ERROR,
            f"no memory.db at {db_path} -- run memory_rebuild first.",
        )
    if old_id == new_id:
        return _err(ErrorCode.INVALID_PARAMS, "old_id and new_id must be different.")
    try:
        from save_pipeline import memory_supersede_db

        success, error_msg = memory_supersede_db(
            db_path, old_id, new_id, valid_to=valid_to
        )
        if not success:
            if error_msg and "temporal columns" in error_msg:
                return _err(
                    ErrorCode.SCHEMA_MISSING,
                    f"memory schema does not have temporal columns. Run the migration: {GLOBAL_SCRIPTS_DIR}/migrate_temporal_validity.py",
                )
            if error_msg and "not found" in error_msg:
                return _err(ErrorCode.NOT_FOUND, f"{error_msg} in {db_path}.")
            return _err(ErrorCode.DB_ERROR, error_msg or "unknown error")
        old_md_path = target_base / f"{old_id}.md"
        if old_md_path.exists():
            try:
                txt = old_md_path.read_text(encoding="utf-8")
                txt = re.sub(
                    r"^valid_to:\s*null\s*$",
                    f"valid_to: {valid_to}",
                    txt,
                    flags=re.MULTILINE,
                )
                if "valid_to:" not in txt:
                    txt = re.sub(
                        r"^(valid_from:.*)$",
                        rf"\1\nvalid_to: {valid_to}",
                        txt,
                        count=1,
                        flags=re.MULTILINE,
                    )
                txt = re.sub(
                    r"^superseded_by:\s*null\s*$",
                    f"superseded_by: {new_id}",
                    txt,
                    flags=re.MULTILINE,
                )
                if "superseded_by:" not in txt:
                    txt = txt.rstrip() + f"\nsuperseded_by: {new_id}\n"
                atomic_write(old_md_path, txt, encoding="utf-8")
            except Exception as _e:
                # FLAVOR_B: if the .md frontmatter update fails, the DB
                # will still mark the note as superseded below. That
                # creates DB/filesystem divergence — the .md file
                # still shows the note as active. Log loudly so the
                # operator can reconcile. (Fix from FLAVOR_B audit
                # pass 2026-06-20.)
                logger.warning(
                    "memory_supersede: failed to update frontmatter in %s "
                    "(DB will still mark as superseded): %s",
                    old_md_path,
                    _e,
                )
        _search_cache.clear()
        return (
            f"Superseded: {old_id} is now valid_to={valid_to}, superseded_by={new_id}."
        )
    except Exception as e:
        logger.exception("in memory_supersede")
        return _err(ErrorCode.DB_ERROR, "in memory_supersede")


@mcp.tool()
@with_audit("memory_auto_save_hook")
def memory_auto_save_hook(
    tool: str, params_json: str = "", result_preview: str = ""
) -> str:
    """Save one tool invocation as an auto-save session note."""
    script = GLOBAL_SCRIPTS_DIR / "auto_save.py"
    if not script.exists():
        return _err(ErrorCode.NOT_FOUND, f"auto_save.py not found at {script}.")
    try:
        out, _ = _run_subprocess_output(
            [
                sys.executable,
                str(script),
                "tool-complete",
                "--tool",
                tool,
                "--params",
                params_json,
                "--result-preview",
                result_preview,
            ],
            timeout=10,
            cwd=str(GLOBAL_SCRIPTS_DIR),
        )
        if out.startswith("[stderr]") or "[stderr]" in out[:50]:
            return f"Auto-save stderr: {out[:300]}"
        try:
            data = json.loads(out)
            if data.get("saved"):
                _search_cache.clear()
                return f"Auto-saved: {data['note_id']}"
            return f"Auto-save failed: {data.get('error', out[:200])}"
        except (json.JSONDecodeError, KeyError):
            return f"Auto-save returned: {out[:200]}"
    except subprocess.TimeoutExpired:
        return _err(
            ErrorCode.TIMEOUT, "auto_save.py tool-complete timed out after 10s."
        )


@mcp.tool()
@with_audit("memory_daily_digest")
def memory_daily_digest(date: str = "") -> str:
    """Roll all auto-save notes for a given date into one daily note."""
    script = GLOBAL_SCRIPTS_DIR / "auto_save.py"
    if not script.exists():
        return _err(ErrorCode.NOT_FOUND, f"auto_save.py not found at {script}.")
    try:
        cmd = [sys.executable, str(script), "daily-digest"]
        if date:
            cmd.extend(["--date", date])
        out, _ = _run_subprocess_output(cmd, timeout=30, cwd=str(GLOBAL_SCRIPTS_DIR))
        if out.startswith("[stderr]") or "[stderr]" in out[:50]:
            return f"Daily-digest stderr: {out[:300]}"
        try:
            data = json.loads(out)
            if data.get("digested", 0) > 0:
                _search_cache.clear()
            return json.dumps(data, indent=2)
        except (json.JSONDecodeError, KeyError):
            return f"Daily-digest returned: {out[:300]}"
    except subprocess.TimeoutExpired:
        return _err(ErrorCode.TIMEOUT, "auto_save.py daily-digest timed out after 30s.")


@mcp.tool()
@with_audit("memory_auto_save_status")
def memory_auto_save_status() -> str:
    """Count auto-save notes from the last 24h, 7d, and per-day breakdown."""
    script = GLOBAL_SCRIPTS_DIR / "auto_save.py"
    if not script.exists():
        return _err(ErrorCode.NOT_FOUND, f"auto_save.py not found at {script}.")
    try:
        out, _ = _run_subprocess_output(
            [sys.executable, str(script), "status"],
            timeout=10,
            cwd=str(GLOBAL_SCRIPTS_DIR),
        )
        if out.startswith("[stderr]") or "[stderr]" in out[:50]:
            return f"auto_save status stderr: {out[:300]}"
        return str(out)
    except subprocess.TimeoutExpired:
        return _err(ErrorCode.TIMEOUT, "auto_save.py status timed out after 10s.")


@mcp.tool()
@with_audit("memory_auto_save_daemon_metrics")
def memory_auto_save_daemon_metrics() -> str:
    """Get auto-save daemon metrics (buffer size, circuit state, inbox size, etc.)."""
    try:
        from auto_save import (
            _AUTO_SAVE_STATE,
            _AUTO_SAVE_STATE_LOCK,
            _auto_save_circuit_open,
        )
    except Exception as e:
        return _err(ErrorCode.DB_ERROR, f"Failed to import auto_save: {e}")

    # Get daemon state
    import time

    inbox_path = None
    try:
        from auto_save import get_auto_save_inbox_path, get_db_path

        inbox_path = get_auto_save_inbox_path()
    except Exception:
        pass

    with _AUTO_SAVE_STATE_LOCK:
        state = {
            "failure_count": len(_AUTO_SAVE_STATE["failure_times"]),
            "circuit_open_until": _AUTO_SAVE_STATE["circuit_open_until"],
            "circuit_open": time.time() < _AUTO_SAVE_STATE["circuit_open_until"],
            "last_backoff_seconds": _AUTO_SAVE_STATE["last_backoff_seconds"],
        }

    inbox_size = 0
    if inbox_path and inbox_path.exists():
        try:
            inbox_size = inbox_path.stat().st_size
        except OSError:
            pass

    import json

    return json.dumps(
        {
            "circuit_breaker": state,
            "inbox_size_bytes": inbox_size,
            "inbox_path": str(inbox_path) if inbox_path else None,
            "timestamp": time.time(),
        },
        indent=2,
    )


@mcp.tool()
@with_audit("memory_reinforce")
def memory_reinforce(memory_ids: list, success: bool) -> str:
    """Reinforce memory success scores based on outcome.

    G4 fix (2026-06-22): this is the *explicit* outcome signal (see
    ``mcp_ctr_drift.memory_record_ctr_feedback`` for the *implicit*
    signal).  Call when a user or downstream agent confirms a
    memory was correct (``success=True``) or wrong (``success=False``).
    Do NOT call on every "user saw the result" event — use
    ``memory_record_ctr_feedback`` for that.
    """
    delta = 1.0 if success else -1.0
    try:
        by_db: dict[str, list[str]] = {}
        not_found: list[str] = []
        for mid in memory_ids:
            db_path_override = os.environ.get("MEMORY_DB_PATH")
            if db_path_override:
                db_path: Path = Path(db_path_override)
            else:
                resolved = resolve_db_for_memory_id(mid)
                if resolved is None:
                    not_found.append(mid)
                    continue
                db_path = resolved
            by_db.setdefault(str(db_path), []).append(mid)

        if not by_db:
            if not_found:
                return f"No memories found to reinforce: {not_found}"
            return "No memory_ids provided."

        updated_total = 0
        hits_per_db: dict[str, int] = {}
        for db_path_key, ids in by_db.items():
            from save_pipeline import reinforce_memories_db

            hits = reinforce_memories_db(Path(db_path_key), ids, delta)
            hits_per_db[str(db_path_key)] = hits
            updated_total += hits
        _search_cache.clear()
        scope_note = []
        for db_path_key, hits in hits_per_db.items():
            if hits:
                label = "global" if str(GLOBAL_MEM_DIR) in str(db_path_key) else "local"
                scope_note.append(f"{hits} in {label}")
        _search_cache.clear()
        return (
            f"Successfully reinforced {updated_total} memories with outcome success={success} "
            f"({', '.join(scope_note) or 'no matches'}; fitness scores recalculated)."
        )
    except Exception as e:
        logger.exception("reinforcing outcomes")
        return _err(ErrorCode.DB_ERROR, "reinforcing outcomes")


@mcp.tool()
@with_audit("memory_delete")
def memory_delete(note_id: str, hard: bool = False) -> str:
    """Soft-delete a memory by note_id. With hard=True, immediately purges."""
    try:
        from memory_delete import soft_delete_note, hard_delete_note

        try:
            active_dir = _resolve_memory_dir()
        except Exception:
            active_dir = None
        if active_dir is None:
            return _err(ErrorCode.DB_ERROR, "No active memory directory found.")
        db_path = active_dir / "memory.db"
        if not db_path.exists():
            return _err(
                ErrorCode.DB_ERROR,
                f"memory.db not found at {db_path} -- run memory_rebuild first.",
            )
        if hard:
            ok = hard_delete_note(db_path, note_id)
            return (
                f"Hard-deleted {note_id}"
                if ok
                else _err(ErrorCode.NOT_FOUND, f"{note_id} not found or still active")
            )
        ok = soft_delete_note(db_path, note_id, deleted_by="user")
        return (
            f"Soft-deleted {note_id} (30-day restore window)"
            if ok
            else _err(ErrorCode.NOT_FOUND, f"{note_id} not found or already deleted")
        )
    except ValueError as ve:
        return _err(ErrorCode.INVALID_PARAMS, str(ve))
    except Exception as e:
        logger.exception("memory_delete failed")
        return _err(ErrorCode.DB_ERROR, "Delete failed")


@mcp.tool()
@with_audit("memory_restore")
def memory_restore(note_id: str) -> str:
    """Restore a soft-deleted memory by note_id. Within the 30-day window."""
    try:
        from memory_delete import restore_note

        try:
            active_dir = _resolve_memory_dir()
        except Exception:
            active_dir = None
        if active_dir is None:
            return _err(ErrorCode.DB_ERROR, "No active memory directory found.")
        db_path = active_dir / "memory.db"
        if not db_path.exists():
            return _err(
                ErrorCode.DB_ERROR,
                f"memory.db not found at {db_path} -- run memory_rebuild first.",
            )
        ok = restore_note(db_path, note_id)
        return (
            f"Restored {note_id}"
            if ok
            else _err(ErrorCode.NOT_FOUND, f"{note_id} not found or not deleted")
        )
    except ValueError as ve:
        return _err(ErrorCode.INVALID_PARAMS, str(ve))
    except Exception as e:
        logger.exception("memory_restore failed")
        return _err(ErrorCode.DB_ERROR, "Restore failed")


@mcp.tool()
@with_audit("memory_trash")
def memory_trash(include_expired: bool = False) -> str:
    """List soft-deleted memories, oldest first. Excludes 30-day-expired by default."""
    try:
        from memory_delete import list_trash

        try:
            active_dir = _resolve_memory_dir()
        except Exception:
            active_dir = None
        if active_dir is None:
            return _err(ErrorCode.DB_ERROR, "No active memory directory found.")
        db_path = active_dir / "memory.db"
        if not db_path.exists():
            return _err(
                ErrorCode.DB_ERROR,
                f"memory.db not found at {db_path} -- run memory_rebuild first.",
            )
        items = list_trash(db_path, include_expired=include_expired)
        if not items:
            return "Trash is empty."
        lines = [
            f"[{i + 1}] {it['id']}  deleted {it['deleted_at']}  by {it['deleted_by']}  (purge in {it['days_until_purge']:.1f}d)"
            for i, it in enumerate(items)
        ]
        return f"Trash ({len(items)} items):\n" + "\n".join(lines)
    except Exception as e:
        logger.exception("memory_trash failed")
        return _err(ErrorCode.DB_ERROR, "List trash failed")


@mcp.tool()
@with_audit("memory_purge_expired")
def memory_purge_expired() -> str:
    """Hard-delete all soft-deleted memories older than 30 days. Returns count."""
    try:
        from memory_delete import purge_expired

        try:
            active_dir = _resolve_memory_dir()
        except Exception:
            active_dir = None
        if active_dir is None:
            return _err(ErrorCode.DB_ERROR, "No active memory directory found.")
        db_path = active_dir / "memory.db"
        if not db_path.exists():
            return _err(
                ErrorCode.DB_ERROR,
                f"memory.db not found at {db_path} -- run memory_rebuild first.",
            )
        n = purge_expired(db_path)
        return f"Purged {n} expired note(s)."
    except Exception as e:
        logger.exception("memory_purge_expired failed")
        return _err(ErrorCode.DB_ERROR, "Purge failed")


@mcp.tool()
@with_audit("memory_purge_auto_saves")
def memory_purge_auto_saves(dry_run: bool = False) -> str:
    """Delete all auto-saved tool-log entries from DB and disk.

    Removes the ~3400+ zero-importance entries created by the old
    firehose-style auto-save hook. Use ``dry_run=True`` to preview.
    """
    from auto_save import purge_auto_saves

    result = purge_auto_saves(dry_run=dry_run)
    return json.dumps(result, indent=2)
