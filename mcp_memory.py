from __future__ import annotations
"""
Memory CRUD MCP tools — save, superseede, delete, restore, trash, purge, auto_save*, daily_digest, reinforce.
"""
from mcp_common import _bootstrap_path  # noqa: E402,F401


import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from infra.infrastructure import _err, ErrorCode
from mcp_common import (
    _resolve_memory_dir,
    _run_subprocess_output,
    _search_cache,
    GLOBAL_SCRIPTS_DIR,
    GLOBAL_MEM_DIR,
    get_memory_paths,
    logger,
    with_audit,
    atomic_write,
    resolve_db_for_memory_id,
)
from mcp_instance import mcp


def _resolve_principal_for_rbac() -> tuple[str | None, str | None]:
    """Resolve (principal_id, tenant_id) from agent context for RBAC checks."""
    principal_id: str | None = None
    try:
        from agent_context import get_agent
        ctx = get_agent()
        principal_id = getattr(ctx, "principal_id", None) or getattr(ctx, "agent_id", None)
        if principal_id:
            principal_id = str(principal_id).lower()
    except (ImportError, Exception):
        pass
    tenant_id: str | None = None
    if principal_id:
        try:
            from infra.authorizer import resolve_tenant_for_principal
            tenant_id = resolve_tenant_for_principal(principal_id)
        except Exception:
            tenant_id = principal_id
    return principal_id, tenant_id
from save_pipeline import save_memory, SaveValidationError


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
    """Save a memory note to persistent offline storage and update the search index.

    USE THIS TOOL WHEN:
    - You want to remember a significant lesson learned, design decision, user preference, project detail, or session summary for future interactions.
    - You want to persist information that should span across sessions/conversations.

    DO NOT USE THIS TOOL FOR:
    - Storing transient conversation context (that fits in the short-term window).

    ARGUMENTS:
    - content: The substantive text of the memory (markdown format is encouraged). Keep it detailed and self-contained.
    - category: The directory where the memory will be stored. Choose from:
        * 'lessons': general rules, coding guidelines, or lessons learned.
        * 'decisions': architectural or technical decisions with rationale.
        * 'projects': details about specific projects or features being built.
        * 'preferences': user-specific preferences, constraints, or styles.
        * 'sessions': high-level summaries of conversation threads.
    - title_slug: A URL-friendly alphanumeric name for the file (e.g. 'setup_nextjs_auth_v2').
    - tags: Optional list of keyword tags for search indexing.
    - pinned: If True, locks the memory to the 'hot' tier (preventing automatic decay/archival). Use only for critical, permanent rules/context.
    - is_global: If True, saves to the global configuration memory path (available to all workspaces). If False, saves to the workspace-specific local memory.
    - importance: Integer 1-5 (default 3) indicating importance level. Higher levels boost ranking and slow down decay.

    RETURNS:
    A status string indicating whether the write succeeded and the path of the saved memory file.
    """
    if len(content) > 100_000:
        return _err(ErrorCode.INVALID_PARAMS, f"content exceeds 100,000 character limit ({len(content)} chars)")

    # RBAC: check write authorization before saving
    try:
        from infra.authorizer import mcp_authorize, log_authorization_decision
        principal_id, tenant_id = _resolve_principal_for_rbac()
        auth_db = None
        try:
            from infra.memory_common import get_memory_paths
            _, local_mem, _ = get_memory_paths()
            auth_db = str(local_mem / "memory.db") if (local_mem / "memory.db").exists() else None
        except Exception:
            pass
        if not mcp_authorize(principal_id, "write", "memory", auth_db, tenant_id=tenant_id):
            log_authorization_decision(
                principal_id=principal_id,
                action="write",
                resource="memory",
                allowed=False,
                db_path=auth_db,
                tenant_id=tenant_id,
            )
            return _err(
                ErrorCode.AUTHORIZATION_DENIED,
                f"Not authorized to save memory. "
                f"Principal '{principal_id or 'anonymous'}' lacks the required role.",
            )
    except ImportError:
        pass

    # Coordination: acquire file lock + update project state before save
    _coord_lock_path = None
    try:
        from coordination.hooks import (
            acquire_save_lock, update_project_activity,
            queue_lock_conflict_message,
        )
        from infra.infrastructure import resolve_active_memory_dir
        _coord_lock_path = str((resolve_active_memory_dir() / "memory" / category / f"{title_slug}.md").resolve())
        locked = acquire_save_lock(_coord_lock_path)
        if not locked.get("acquired", locked if isinstance(locked, bool) else False):
            # Lock held by another agent — send conflict message, proceed anyway
            try:
                from coordination.locking import check_lock
                import coordination.hooks as _ch
                _cl_conn = _ch._make_conn()
                lock_info = check_lock(_cl_conn, _coord_lock_path) if _cl_conn else None
                if lock_info:
                    queue_lock_conflict_message(
                        _coord_lock_path, lock_info["locked_by"],
                        _ch._get_agent_id(),
                    )
            except Exception:
                pass
        update_project_activity(_coord_lock_path, activity="writing")
    except Exception:
        _coord_lock_path = None

    try:
        # C1: route through the durable CQRS write-journal when the
        # feature flag is enabled.  The journal path performs a lock-free
        # INSERT into journal.db (WAL) and returns the note_id
        # immediately; the reconciliation daemon (the single writer to
        # memory.db) materializes it asynchronously.  This keeps the MCP
        # verb off the direct memory.db write path, which would race the
        # daemon.  When the flag is off, fall back to the direct
        # save_memory (local/single-writer) for backward compatibility.
        from config import get_config
        from save_pipeline import save_memory_journal

        cfg = get_config()
        if getattr(cfg, "write_journal", False):
            result = save_memory_journal(
                content=content,
                category=category,
                title_slug=title_slug,
                tags=tags or [],
                pinned=pinned,
                is_global=is_global,
                importance=importance,
                context="mcp",
                note_id="",
                defer_expensive=True,
            )
            # save_memory_journal returns the note_id (or an _err
            # envelope string on hard failure).  Preserve that semantics.
            return str(result)
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
            defer_expensive=True,
        )
    except SaveValidationError as e:
        return str(e)
    finally:
        # Coordination: release file lock + clear project activity
        if _coord_lock_path:
            try:
                from coordination.hooks import release_save_lock, clear_project_activity
                release_save_lock(_coord_lock_path)
                clear_project_activity(_coord_lock_path)
            except Exception:
                pass
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
    """Mark an existing memory note as outdated and superseded by a newer memory.

    USE THIS TOOL WHEN:
    - You are updating/replacing a previous memory note (e.g. updating a project design or user preference).
    - You want to ensure the old memory is excluded from searches by default, but preserved in the historical timeline.

    ARGUMENTS:
    - old_id: The ID (category/title_slug) of the old memory to supersede (e.g. 'preferences/editor_font_size').
    - new_id: The ID (category/title_slug) of the new memory replacing it (e.g. 'preferences/editor_font_size_v2').
    - valid_to: Optional ISO 8601 string (e.g. '2026-06-29T12:00:00Z'). Defaults to current time if not provided.

    RETURNS:
    A status string indicating whether the supersession was recorded successfully.
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
        try:
            from infra.cache import invalidate_cache_for_note

            invalidate_cache_for_note(old_id)
            if new_id:
                invalidate_cache_for_note(new_id)
        except Exception as e:
            logger.warning("Unhandled exception in memory_supersede: %s", e)
            _search_cache.clear()
        return (
            f"Superseded: {old_id} is now valid_to={valid_to}, superseded_by={new_id}."
        )
    except Exception:
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
                try:
                    from infra.cache import invalidate_cache_for_note

                    invalidate_cache_for_note(data["note_id"])
                except Exception as e:
                    logger.warning("Unhandled exception in memory_auto_save_hook: %s", e)
                    _search_cache.clear()
                return f"Auto-saved: {data['note_id']}"
            return _err(ErrorCode.DB_ERROR, f"Auto-save failed: {data.get('error', out[:200])}")
        except (json.JSONDecodeError, KeyError):
            return _err(ErrorCode.INVALID_PARAMS, f"Auto-save returned: {out[:200]}")
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
        from background.auto_save import (
            _AUTO_SAVE_STATE,
            _AUTO_SAVE_STATE_LOCK,
        )
    except Exception as e:
        logger.warning("Unhandled exception in memory_auto_save_daemon_metrics: %s", e)
        return _err(ErrorCode.DB_ERROR, f"Failed to import auto_save: {e}")

    # Get daemon state

    inbox_path = None
    try:
        from background.auto_save import get_auto_save_inbox_path

        inbox_path = get_auto_save_inbox_path()
    except Exception as e:
        logger.warning("Unhandled exception in memory_auto_save_daemon_metrics: %s", e)

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
        except OSError as exc:
            logger.debug("mcp_memory: cannot stat inbox %s: %s", inbox_path, exc)

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
    # Implicit click signal: when a memory is reinforced as successful,
    # stamp clicked_at on the most recent CTR impression for that memory
    # so LTR has training data.
    if success:
        try:
            import time as _time
            _db_path = str(_resolve_memory_dir() / "memory.db")
            from infra._lazy_imports import connection_pool, safe_close_db
            _conn = connection_pool.get(_db_path, tenant_id="default")
            try:
                for mid in memory_ids:
                    try:
                        _conn.execute(
                            "UPDATE memory_ctr_feedback "
                            "SET clicked_at = COALESCE(clicked_at, ?) "
                            "WHERE id = ? AND clicked_at IS NULL "
                            "AND returned_at = ("
                            "  SELECT MAX(returned_at) FROM memory_ctr_feedback "
                            "  WHERE id = ?"
                            ")",
                            (_time.time(), mid, mid),
                        )
                    except Exception:
                        pass
                _conn.commit()
            finally:
                safe_close_db(_conn)
        except Exception:
            pass
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
        try:
            from infra.cache import invalidate_cache_for_note

            all_ids = [mid for ids in by_db.values() for mid in ids]
            for nid in all_ids:
                invalidate_cache_for_note(nid)
        except Exception as e:
            logger.warning("Unhandled exception in memory_reinforce: %s", e)
            _search_cache.clear()
        scope_note = []
        for db_path_key, hits in hits_per_db.items():
            if hits:
                label = "global" if str(GLOBAL_MEM_DIR) in str(db_path_key) else "local"
                scope_note.append(f"{hits} in {label}")
        return (
            f"Successfully reinforced {updated_total} memories with outcome success={success} "
            f"({', '.join(scope_note) or 'no matches'}; fitness scores recalculated)."
        )
    except Exception:
        logger.exception("reinforcing outcomes")
        return _err(ErrorCode.DB_ERROR, "reinforcing outcomes")


@with_audit("memory_delete")
def memory_delete(note_id: str, hard: bool = False) -> str:
    """Soft-delete or hard-purge a memory note by ID.

    USE THIS TOOL WHEN:
    - You want to delete a memory that is incorrect, outdated, or no longer needed.
    - Soft-delete (default) keeps the note in a recycle bin (trash) for 30 days before permanent deletion, enabling restoration.
    - Hard-purge (hard=True) deletes it from the disk and database immediately and irreversibly.

    ARGUMENTS:
    - note_id: The ID of the memory note to delete (e.g. 'lessons/old_rules_v1').
    - hard: If True, executes an immediate hard-delete. Default is False.

    RETURNS:
    A status string indicating whether the deletion succeeded.
    """
    try:
        from memory_delete import soft_delete_note, hard_delete_note

        # RBAC: check delete authorization before proceeding
        try:
            from infra.authorizer import mcp_authorize, log_authorization_decision
            from infra.memory_common import get_memory_paths
            from pathlib import Path

            # Resolve DB path for RBAC check
            _, local_mem, _ = get_memory_paths()
            auth_db = str(local_mem / "memory.db") if (local_mem / "memory.db").exists() else None
            principal_id, tenant_id = _resolve_principal_for_rbac()
            if not mcp_authorize(principal_id, "delete", "memory", auth_db, tenant_id=tenant_id):
                log_authorization_decision(
                    principal_id=principal_id,
                    action="delete",
                    resource="memory",
                    allowed=False,
                    db_path=auth_db,
                    tenant_id=tenant_id,
                )
                return _err(
                    ErrorCode.AUTHORIZATION_DENIED,
                    f"Not authorized to delete note '{note_id}'. "
                    f"Principal '{principal_id or 'anonymous'}' lacks the required role.",
                )
        except Exception:
            # Fail-open: if RBAC subsystem is unavailable, allow the operation
            pass

        try:
            active_dir = _resolve_memory_dir()
        except Exception as e:
            logger.warning("Unhandled exception in memory_delete: %s", e)
            active_dir = None
        if active_dir is None:
            return _err(ErrorCode.DB_ERROR, "No active memory directory found.")
        db_path = active_dir / "memory.db"
        if not db_path.exists():
            return _err(
                ErrorCode.DB_ERROR,
                f"memory.db not found at {db_path} -- run memory_rebuild first.",
            )
        from infra.db_path_flock import db_path_flock
        with db_path_flock(db_path):
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
    except Exception:
        logger.exception("memory_delete failed")
        return _err(ErrorCode.DB_ERROR, "Delete failed")


@mcp.tool()
@with_audit("memory_restore")
def memory_restore(note_id: str) -> str:
    """Restore a soft-deleted memory note from the trash.

    USE THIS TOOL WHEN:
    - You accidentally soft-deleted a memory note and want to recover it.
    - Soft-deleted notes remain in the trash for up to 30 days before they are purged forever.

    ARGUMENTS:
    - note_id: The ID of the soft-deleted memory note to restore (e.g. 'lessons/old_rules_v1').

    RETURNS:
    A status string indicating whether the note was successfully restored.
    """
    try:
        from memory_delete import restore_note

        # RBAC: check write authorization before proceeding
        try:
            from infra.authorizer import mcp_authorize, log_authorization_decision
            from infra.memory_common import get_memory_paths
            from pathlib import Path

            _, local_mem, _ = get_memory_paths()
            auth_db = str(local_mem / "memory.db") if (local_mem / "memory.db").exists() else None
            principal_id, tenant_id = _resolve_principal_for_rbac()
            if not mcp_authorize(principal_id, "write", "memory", auth_db, tenant_id=tenant_id):
                log_authorization_decision(
                    principal_id=principal_id,
                    action="write",
                    resource="memory",
                    allowed=False,
                    db_path=auth_db,
                    tenant_id=tenant_id,
                )
                return _err(
                    ErrorCode.AUTHORIZATION_DENIED,
                    f"Not authorized to restore note '{note_id}'.",
                )
        except Exception:
            pass

        try:
            active_dir = _resolve_memory_dir()
        except Exception as e:
            logger.warning("Unhandled exception in memory_restore: %s", e)
            active_dir = None
        if active_dir is None:
            return _err(ErrorCode.DB_ERROR, "No active memory directory found.")
        db_path = active_dir / "memory.db"
        if not db_path.exists():
            return _err(
                ErrorCode.DB_ERROR,
                f"memory.db not found at {db_path} -- run memory_rebuild first.",
            )
        from infra.db_path_flock import db_path_flock
        with db_path_flock(db_path):
            ok = restore_note(db_path, note_id)
            return (
                f"Restored {note_id}"
                if ok
                else _err(ErrorCode.NOT_FOUND, f"{note_id} not found or not deleted")
            )
    except ValueError as ve:
        return _err(ErrorCode.INVALID_PARAMS, str(ve))
    except Exception:
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
        except Exception as e:
            logger.warning("Unhandled exception in memory_trash: %s", e)
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
    except Exception:
        logger.exception("memory_trash failed")
        return _err(ErrorCode.DB_ERROR, "List trash failed")


@mcp.tool()
@with_audit("memory_purge_expired")
def memory_purge_expired(dry_run: bool = False) -> str:
    """Hard-delete all soft-deleted memories older than 30 days. Returns count."""
    try:
        from memory_delete import purge_expired

        try:
            active_dir = _resolve_memory_dir()
        except Exception as e:
            logger.warning("Unhandled exception in memory_purge_expired: %s", e)
            active_dir = None
        if active_dir is None:
            return _err(ErrorCode.DB_ERROR, "No active memory directory found.")
        db_path = active_dir / "memory.db"
        if not db_path.exists():
            return _err(
                ErrorCode.DB_ERROR,
                f"memory.db not found at {db_path} -- run memory_rebuild first.",
            )
        n = purge_expired(db_path, dry_run=dry_run)
        if dry_run:
            return f"[DRY RUN] Would purge {n} expired note(s)."
        return f"Purged {n} expired note(s)."
    except Exception:
        logger.exception("memory_purge_expired failed")
        return _err(ErrorCode.DB_ERROR, "Purge failed")


@mcp.tool()
@with_audit("memory_purge_auto_saves")
def memory_purge_auto_saves(dry_run: bool = False) -> str:
    """Delete all auto-saved tool-log entries from DB and disk.

    Removes the ~3400+ zero-importance entries created by the old
    firehose-style auto-save hook. Use ``dry_run=True`` to preview.
    """
    from background.auto_save import purge_auto_saves

    result = purge_auto_saves(dry_run=dry_run)
    return json.dumps(result, indent=2)
