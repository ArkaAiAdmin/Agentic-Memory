"""17-tool agent surface for agentic memory.

Each tool is a thin @mcp.tool() wrapper around existing functionality
with sensible defaults so the agent can call it with 1-2 params.

The underlying ADMIN tools are still accessible via
memory_advanced(operation="...") for power users.

Tools (17 + 1 escape hatch (memory_advanced)):
  memory_search, memory_save, memory_delete, memory_recall, memory_note,
  memory_learn, memory_audit, memory_organize, memory_share,
  memory_graph, memory_profile, memory_session_start, memory_review_beliefs,
  memory_curate_autosave, memory_health_check, memory_system_health,
  memory_advanced

Phase A (2026-07-01): These 17 tools are the entire agent-facing MCP
surface. The 80+ legacy tools are callable through memory_advanced.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import cast

from mcp_common import (
    GLOBAL_MEM_DIR,
    _err,
    classify_exception,
    ErrorCode,
    get_memory_paths,
    logger,
    with_audit,
)
from mcp_instance import mcp

# ---------------------------------------------------------------------------
# RBAC authorization helpers
# ---------------------------------------------------------------------------

def _resolve_auth_db_path() -> str | None:
    """Resolve the DB path for RBAC authorization checks."""
    try:
        db_path = _resolve_db_path()
        return str(db_path) if db_path and db_path.exists() else None
    except Exception:
        return None


def _get_principal_from_context() -> str | None:
    """Resolve the current principal for RBAC + audit logging.

    The :class:`~agent_context.AgentContext` dataclass carries the
    per-thread agent identity (``agent_id``, ``display_name``, etc.).
    ``principal_id`` is not a native field on that dataclass; the agent
    identity and the principal are one and the same in the current
    implementation, so ``agent_id`` is returned as the principal id.

    Returns ``None`` when no agent context has been activated (e.g.
    unauthenticated mode).
    """
    try:
        from agent_context import get_agent

        ctx = get_agent()
        agent_id = getattr(ctx, "agent_id", None)
        if agent_id:
            return str(agent_id)
    except (ImportError, Exception):
        pass
    return None


def _check_authorization(action: str, resource: str = "memory") -> str | None:
    """Check RBAC authorization. Returns error string if denied, None if allowed.

    Fail-open: returns None (allow) on any error or when no RBAC is configured.
    """
    try:
        from infra.authorizer import mcp_authorize, log_authorization_decision

        principal_id = _get_principal_from_context()
        db_path = _resolve_auth_db_path()

        allowed = mcp_authorize(
            principal_id=principal_id,
            action=action,
            resource=resource,
            db_path=db_path,
        )
        if not allowed:
            log_authorization_decision(
                principal_id=principal_id,
                action=action,
                resource=resource,
                allowed=False,
                db_path=db_path,
            )
            return _err(
                ErrorCode.AUTHORIZATION_DENIED,
                f"Not authorized for '{action}' on '{resource}'. "
                f"Principal '{principal_id or 'anonymous'}' lacks the required role.",
            )
        # Log successful authorization too
        log_authorization_decision(
            principal_id=principal_id,
            action=action,
            resource=resource,
            allowed=True,
            db_path=db_path,
        )
        return None
    except Exception as exc:
        # Fail-closed: RBAC errors must not grant access
        logger.warning("_check_authorization failed (fail-closed): %s", exc)
        return _err(
            ErrorCode.AUTHORIZATION_DENIED,
            f"RBAC check failed for '{action}' on '{resource}'. Authorization denied (fail-closed).",
        )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_db_path(is_global: bool = False, db_path: str | None = None):
    """Resolve the active memory DB path."""
    if db_path:
        return Path(db_path)
    if is_global:
        return GLOBAL_MEM_DIR / "memory.db"
    _, local_mem, _ = get_memory_paths()
    return local_mem / "memory.db"


def _auto_slug(content: str) -> str:
    """Generate a short slug from content for auto-naming."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    h = hashlib.md5(content.encode()).hexdigest()[:4]
    return f"auto-{ts}-{h}"


def _wrap_db_error(verb_name: str, e: Exception) -> str:
    """Classify a DB exception and return a structured error envelope."""
    code = classify_exception(e)
    return _err(code, f"{verb_name}: {e}")


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------


@mcp.tool()
@with_audit("memory_search")
def memory_search(
    query: str,
    category: str = "",
    limit: int = 10,
    include_global: bool | None = None,
    mode: str = "hybrid",
    tenant_id: str = "default",
    belief_status: str | None = None,
    epistemic_source: str | None = None,
    fact_type: str | None = None,
    memory_source: str | None = None,
    shared_with_me: bool = False,
) -> str:
    """Search memories by semantic + FTS5 hybrid search.

    The primary recall tool. Returns ranked memories matching the query.
    When the CQRS write journal is enabled, pending (not-yet-materialized)
    entries are also checked and surfaced as a supplement when the main
    search returns no results.

    Args:
        query: Natural-language search query (required).
        category: Filter to a category (e.g. "lessons", "decisions").
        limit: Max results (default 10).
        include_global: Include global memories (default True for default agent,
            scoped for non-default agents).
        mode: "hybrid" (default), "semantic", "fts", "facts", "graph".
        belief_status: Filter KG facts by belief status (active, retracted, deprecated, unconfirmed).
        epistemic_source: Filter KG facts by epistemic source (agent, auto_save, hook, import, cron).
        fact_type: Filter KG facts by type (observation, agent_inference, external_stated, hypothesis, derived).
        memory_source: Filter memories by source type ("agent", "auto_save", "import"). Only returns
            memories whose source file category matches the given type.
        shared_with_me: If True, also include memories explicitly shared with
            the current agent (via the shared pool's target_agent_id).
    """
    auth_err = _check_authorization("search", "memory")
    if auth_err:
        return auth_err
    if include_global is None:
        try:
            from agent_context import get_agent
            _ctx = get_agent()
            include_global = _ctx.namespace == "default"
        except (ImportError, Exception):
            include_global = True
    try:
        from search.orchestrator import search_memories

        db_path = _resolve_db_path()
        result = search_memories(
            db_path=db_path,
            query=query,
            limit=limit,
            include_global=include_global,
            tenant_id=tenant_id,
            belief_status=belief_status,
            epistemic_source=epistemic_source,
            fact_type=fact_type,
            memory_source=memory_source,
            category=category,
            shared_with_me=shared_with_me,
        )
        output = cast(str, result.get("output", str(result)))
        from config import get_config
        if getattr(get_config(), "write_journal", False):
            try:
                pending = _supplement_with_pending(db_path, query, limit)
            except Exception as e:
                logger.warning("Unhandled exception in memory_search: %s", e)
                pending = []
            if pending:
                rows = "\n".join(
                    f"- [{r.get('category','')}/{r.get('title_slug','')}] {r.get('content','')[:120]}"
                    for r in pending
                )
                output = (output or "") + (
                    f"\n\nPending writes (not yet materialized — CQRS journal):\n{rows}"
                )
        return output
    except Exception as e:
        logger.exception("in memory_search verb")
        return _wrap_db_error("memory_search", e)


def _supplement_with_pending(db_path: Path, query: str, limit: int) -> list[dict]:
    """Return recent pending journal entries matching query for read-your-writes visibility."""
    journal_path = db_path.parent / "journal.db"
    if not journal_path.exists():
        return []
    from config import get_config
    if not getattr(get_config(), "write_journal", False):
        return []
    try:
        import sqlite3 as _sqlite3
        _conn = _sqlite3.connect(str(journal_path), timeout=30.0)
        _conn.execute("PRAGMA busy_timeout = 30000;")
        _conn.row_factory = _sqlite3.Row
        _rows = _conn.execute(
            "SELECT note_id, content, category, title_slug, tags, importance, created_at "
            "FROM write_journal WHERE status='pending' AND content LIKE ? "
            "ORDER BY created_at DESC LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        _conn.close()
        return [
            {
                "note_id": r["note_id"],
                "content": r["content"],
                "category": r["category"],
                "title_slug": r["title_slug"],
                "tags": json.loads(r["tags"]) if r["tags"] else [],
                "importance": r["importance"],
                "created_at": r["created_at"],
                "_pending": True,
            }
            for r in _rows
        ]
    except Exception as e:
        logger.warning("Unhandled exception in _supplement_with_pending: %s", e)
        return []


@mcp.tool()
@with_audit("memory_save")
def memory_save(
    content: str,
    category: str = "lessons",
    title_slug: str = "",
    tags: list[str] | None = None,
    pinned: bool = False,
    importance: int = 3,
    is_global: bool = False,
    safety_wiring: bool = True,
) -> str:
    """Save a memory note with sensible defaults.

    Args:
        content: The memory content (markdown).
        category: lessons / decisions / projects / preferences / sessions (default: lessons).
        title_slug: URL-friendly slug (auto-generated if empty).
        tags: Optional keyword tags.
        pinned: Pin to hot tier (default False).
        importance: 1-5 (default 3).
        is_global: Save to global memory (default False).
        safety_wiring: If False, skip prompt-injection scanning (default True).
            Set to False for legitimate structured content with section headers
            and requirement keywords that may trigger false positives.
    """
    auth_err = _check_authorization("write", "memory")
    if auth_err:
        return auth_err
    try:
        from config import get_config
        from infra._lazy_imports import save_memory_journal, save_memory
        from save_pipeline import SaveValidationError

        cfg = get_config()
        _save_fn = save_memory_journal if cfg.write_journal else save_memory

        slug = title_slug or _auto_slug(content)
        try:
            result = _save_fn(
                content=content,
                category=category,
                title_slug=slug,
                tags=tags or [],
                pinned=pinned,
                importance=importance,
                is_global=is_global,
                safety_wiring=safety_wiring,
                defer_expensive=True,
            )
            return str(result)
        except SaveValidationError as e:
            return str(e)
    except Exception as e:
        logger.exception("in memory_save verb")
        return _wrap_db_error("memory_save", e)


@mcp.tool()
@with_audit("memory_review_beliefs")
def memory_review_beliefs(
    min_confidence: float = 0.5,
    belief_status: str = "active",
    older_than_days: float = 30.0,
    limit: int = 20,
) -> str:
    """Review beliefs that may need agent attention — low confidence, old, or stale.

    Returns a structured list of belief assertions with subject/predicate/object
    for the agent to confirm, supersede, retract, or reinforce.

    Args:
        min_confidence: Maximum confidence threshold (returns beliefs BELOW this).
        belief_status: Filter by status (default "active").
        older_than_days: Only return beliefs last reviewed more than this many days ago.
        limit: Max results (default 20).
    """
    auth_err = _check_authorization("search", "memory")
    if auth_err:
        return auth_err
    try:
        from infra.db import open_db
        from belief.belief_lifecycle import get_active_beliefs

        db_path = _resolve_db_path()
        with open_db(db_path, timeout=10.0) as db:
            # Try reading from the review queue first; fall back to filtering
            try:
                rows = db.execute(
                    "SELECT q.belief_id, q.fact_id, ba.confidence, ba.epistemic_source, "
                    "kf.subject, kf.predicate, kf.object "
                    "FROM belief_review_queue q "
                    "JOIN belief_assertions ba ON ba.id = q.belief_id "
                    "LEFT JOIN kg_facts kf ON kf.id = ba.fact_id "
                    "WHERE q.status = 'pending' LIMIT ?", (limit,)
                ).fetchall()
            except Exception:
                rows = []
            candidates = []
            for r in rows:
                candidates.append({
                    "id": r[0],
                    "fact_id": r[1],
                    "confidence": r[2],
                    "epistemic_source": r[3],
                    "subject": r[4],
                    "predicate": r[5],
                    "object": r[6],
                })
            if not candidates:
                # Fallback: filter active beliefs directly
                cutoff = time.time() - (older_than_days * 86400)
                beliefs = get_active_beliefs(
                    db,
                    min_confidence=0,
                    belief_status=belief_status,
                    limit=limit * 2,
                )
                candidates = [
                    b for b in beliefs
                    if b.get("confidence", 1.0) < min_confidence
                    and (b.get("last_reviewed_at") is None or b["last_reviewed_at"] < cutoff)
                ]
            if not candidates:
                return "No beliefs need review at this time."
            lines = [f"Found {len(candidates)} beliefs for review:"]
            for b in candidates:
                subject = b.get("subject", "?")
                predicate = b.get("predicate", "?")
                obj = b.get("object", "?")
                conf = b.get("confidence", 0)
                source = b.get("epistemic_source", "?")
                reviewed = b.get("last_reviewed_at")
                reviewed_str = time.strftime(
                    "%Y-%m-%d", time.localtime(reviewed)
                ) if reviewed else "never"
                lines.append(
                    f"  [{b['id']}] {subject} --[{predicate}]--> {obj}  "
                    f"(confidence={conf:.2f}, source={source}, reviewed={reviewed_str})"
                )
            lines.append(
                "\nUse memory_note(action='supersede', ...) to correct, "
                "or the corresponding belief management tools to confirm/retract."
            )
            return "\n".join(lines)
    except Exception as e:
        logger.exception("in memory_review_beliefs")
        return _wrap_db_error("memory_review_beliefs", e)


@mcp.tool()
@with_audit("memory_curate_autosave")
def memory_curate_autosave(
    start_date: str = "",
    end_date: str = "",
    action: str = "list",
    note_ids: list[str] | None = None,
    category: str = "lessons",
) -> str:
    """Review auto-saved tool invocations and promote or discard them.

    The agent can list auto-saved notes, then batch-promote them into
    intentional lessons or decisions with ``epistemic_source='agent'``.

    Args:
        start_date: ISO date filter start (e.g. "2026-06-01"). Empty = no start bound.
        end_date: ISO date filter end (e.g. "2026-07-01"). Empty = no end bound.
        action: "list" | "promote" | "discard".
        note_ids: List of note IDs to promote/discard (required for promote/discard).
        category: Target category for promotion (default "lessons").
    """
    auth_err = _check_authorization("write", "memory")
    if auth_err:
        return auth_err
    try:
        from pathlib import Path
        from infra.db import open_db

        db_path = _resolve_db_path()

        if action == "list":

            with open_db(db_path, timeout=30.0) as db:
                clauses = ["m.source_file LIKE 'auto_saves/%' AND m.deleted_at IS NULL"]
                params: list = []
                if start_date:
                    clauses.append("m.created_at >= ?")
                    params.append(start_date)
                if end_date:
                    clauses.append("m.created_at <= ?")
                    params.append(end_date + "T23:59:59")

                rows = db.execute(
                    "SELECT m.id, m.content, m.created_at, m.tags "
                    f"FROM tenant_memories m WHERE {' AND '.join(clauses)} "
                    "ORDER BY m.created_at DESC LIMIT 50",
                    params,
                ).fetchall()
                if not rows:
                    return "No auto-saved notes found in the given date range."
                lines = [f"Auto-saved notes ({len(rows)} found):"]
                for r in rows:
                    preview = r[1][:100].replace("\n", " ")
                    lines.append(f"  [{r[0]}] {preview}...")
                return "\n".join(lines)

        if action == "promote":
            if not note_ids:
                return _err(ErrorCode.INVALID_PARAMS, "note_ids required for promote")

            # Read existing notes from DB before mutating, so the
            # data is available after the connection block closes.
            with open_db(db_path, timeout=30.0) as db:
                to_promote: list[tuple[str, tuple]] = []
                for nid in note_ids:
                    row = db.execute(
                        "SELECT content, tags, source_file, title_slug "
                        "FROM tenant_memories WHERE id = ? AND deleted_at IS NULL",
                        (nid,),
                    ).fetchone()
                    if row:
                        to_promote.append((nid, row))

            if not to_promote:
                return "No valid notes to promote."

            from config import get_config
            from infra._lazy_imports import save_memory_journal, save_memory
            from save_pipeline import SaveValidationError
            from mcp_memory import memory_delete

            cfg = get_config()
            _save_fn = save_memory_journal if cfg.write_journal else save_memory

            promoted = 0
            for nid, (content, tags_json, source_file, title_slug) in to_promote:
                slug = title_slug or (
                    Path(source_file).stem if source_file else nid.split("/")[-1]
                )
                tags = json.loads(tags_json) if tags_json else []
                try:
                    # Soft-delete the old auto-save note so the
                    # original record does not co-exist with the
                    # new promoted one (prevents DB/filesystem
                    # divergence and KG orphan rows).
                    memory_delete(note_id=nid, hard=False)
                    # Re-save via write pipeline so .md file write,
                    # FTS5 update, KG extraction, and lock ordering
                    # all happen inside the saga transaction.
                    _save_fn(
                        content=content,
                        category=category,
                        title_slug=slug,
                        tags=tags,
                        pinned=False,
                        importance=3,
                        is_global=False,
                        defer_expensive=True,
                    )
                    promoted += 1
                except SaveValidationError as e:
                    logger.warning("promote validation error for %s: %s", nid, e)
                except Exception as e:
                    logger.warning("Unhandled exception promoting %s: %s", nid, e)
            return f"Promoted {promoted} auto-saved note(s) to '{category}'."

        if action == "discard":
            if not note_ids:
                return _err(ErrorCode.INVALID_PARAMS, "note_ids required for discard")
            from mcp_memory import memory_delete

            discarded = 0
            for nid in note_ids:
                try:
                    memory_delete(note_id=nid, hard=False)
                    discarded += 1
                except Exception as e:
                    logger.warning("discard error for %s: %s", nid, e)
            return f"Discarded {discarded} auto-saved note(s)."
        else:
            return _err(
                ErrorCode.INVALID_PARAMS,
                "action must be 'list', 'promote', or 'discard'",
            )
    except Exception as e:
        logger.exception("in memory_curate_autosave")
        return _wrap_db_error("memory_curate_autosave", e)


@mcp.tool()
@with_audit("memory_delete")
def memory_delete(
    note_id: str,
    hard: bool = False,
    confirm: bool = False,
) -> str:
    """Delete a memory note by ID. Soft-delete by default (recoverable for 30 days).

    Args:
        note_id: The note ID (e.g. "lessons/my-note").
        hard: If True, permanently delete immediately (default False).
        confirm: Required to be True to allow a hard (permanent) delete. This is a
            safety gate: hard deletes cannot be recovered, so they must be explicitly
            confirmed. Soft-deletes (hard=False, the default) are unaffected.
    """
    auth_err = _check_authorization("delete", "memory")
    if auth_err:
        return auth_err
    try:
        from mcp_memory import memory_delete as _delete

        if hard and not confirm:
            logger.warning(
                "Refused permanent (hard) delete of '%s' without explicit confirm=True. "
                "To permanently remove this note, call again with confirm=True.",
                note_id,
            )
            return _err(
                ErrorCode.INVALID_PARAMS,
                f"Refusing hard (permanent) delete of '{note_id}' without confirmation. "
                f"This would permanently remove the note and cannot be recovered. "
                f"Pass confirm=True to proceed with permanent deletion.",
            )

        return str(_delete(note_id=note_id, hard=hard))
    except Exception as e:
        logger.exception("in memory_delete verb")
        return _wrap_db_error("memory_delete", e)


@mcp.tool()
@with_audit("memory_recall")
def memory_recall(query: str = "", session_id: str = "", tenant_id: str = "default") -> str:
    """Recall context for the current session or a named thread.

    Combines session_start + recall_context into one call.
    If no query is given, returns recent session activity.

    Args:
        query: What to recall (default: recent activity).
        session_id: Specific session/thread to recall.
    """
    auth_err = _check_authorization("search", "memory")
    if auth_err:
        return auth_err
    try:
        from search.orchestrator import search_memories

        db_path = _resolve_db_path()
        q = query or "recent session activity"
        try:
            from agent_context import get_agent
            _ctx = get_agent()
            _include_global = _ctx.namespace == "default"
        except (ImportError, Exception):
            _include_global = True
        result = search_memories(
            db_path=db_path,
            query=q,
            limit=5,
            include_global=_include_global,
            tenant_id=tenant_id,
        )
        return str(result.get("output", str(result)))
    except Exception as e:
        logger.exception("in memory_recall verb")
        return _wrap_db_error("memory_recall", e)


@mcp.tool()
@with_audit("memory_note")
def memory_note(
    note_id: str,
    action: str = "read",
    content: str = "",
    category: str = "",
    title_slug: str = "",
    tags: list[str] | None = None,
    rationale: str = "",
    additions: list[str] | None = None,
    deletions: list[str] | None = None,
) -> str:
    """CRUD operations on a specific memory note.

    Sprint 2 additions: ``patch``, ``revert_supersede`` actions + ``rationale`` capture.

    Args:
        note_id: The note ID (e.g. "lessons/my-note").
        action: "read" | "update" | "delete" | "restore" | "supersede" | "patch" | "revert_supersede".
        content: New content (required for update).
        category: New category (for update).
        title_slug: New slug (for update/supersede target).
        tags: New tags (for update).
        rationale: Reason for the action (required for supersede, patch, revert_supersede; recommended for delete).
        additions: Text segments to insert (for patch action).
        deletions: Text segments to remove by content match (for patch action).
    """
    # Map note actions to RBAC actions
    _note_action_map = {
        "read": "search",
        "update": "write",
        "delete": "delete",
        "restore": "write",
        "supersede": "write",
        "patch": "write",
        "revert_supersede": "write",
    }
    rbac_action = _note_action_map.get(action, "write")
    auth_err = _check_authorization(rbac_action, "memory")
    if auth_err:
        return auth_err
    try:
        if action in ("patch", "supersede", "revert_supersede"):
            from config import get_config

            if not getattr(get_config(), 'self_editing', True):
                return _err(
                    ErrorCode.INVALID_PARAMS,
                    f"Self-editing disabled (MEMORY_SELF_EDITING=0). "
                    f"Action '{action}' requires self_editing=True.",
                )
        if action == "read":
            from search.orchestrator import search_memories

            result = search_memories(
                db_path=_resolve_db_path(), query=note_id, limit=1
            )
            return str(result.get("output", str(result)))
        elif action == "delete":
            from mcp_memory import memory_delete

            result = memory_delete(note_id)
            if rationale:
                try:
                    from save_pipeline import _record_revision_log
                    from infra.db import open_db

                    with open_db(_resolve_db_path(), timeout=10.0) as db:
                        _record_revision_log(db, note_id, "delete", rationale=rationale)
                except Exception as e:
                    logger.warning("Unhandled exception in memory_note: %s", e)
            return str(result)
        elif action == "restore":
            from mcp_memory import memory_restore

            result = memory_restore(note_id)
            return str(result)
        elif action == "update":
            from config import get_config
            from infra._lazy_imports import save_memory_journal, save_memory
            from save_pipeline import SaveValidationError

            cfg = get_config()
            _save_fn = save_memory_journal if cfg.write_journal else save_memory

            try:
                result = _save_fn(
                    content=content,
                    category=category or "lessons",
                    title_slug=title_slug or note_id.split("/")[-1],
                    tags=tags or [],
                    importance=3,
                    is_global=False,
                )
                return str(result)
            except SaveValidationError as e:
                return str(e)
        elif action == "supersede":
            if not rationale:
                return _err(ErrorCode.INVALID_PARAMS, "rationale is required for supersede")
            from save_pipeline import memory_supersede_db

            db_path = _resolve_db_path()
            from infra.db_path_flock import db_path_flock
            with db_path_flock(db_path):
                new_note_id = title_slug or note_id
                ok, err = memory_supersede_db(
                    db_path=db_path,
                    old_id=note_id,
                    new_id=new_note_id,
                    rationale=rationale,
                )
            return str(ok) if ok else str(err)
        elif action == "patch":
            if not rationale:
                return _err(ErrorCode.INVALID_PARAMS, "rationale is required for patch")
            from save_pipeline import patch_memory
            from infra.db_path_flock import db_path_flock
            db_path = _resolve_db_path()
            with db_path_flock(db_path):
                patch_result = patch_memory(
                    db_path=db_path,
                    note_id=note_id,
                    additions=additions,
                    deletions=deletions,
                    rationale=rationale,
                )
            return str(patch_result)
        elif action == "revert_supersede":
            from save_pipeline import revert_supersede
            from infra.db_path_flock import db_path_flock
            db_path = _resolve_db_path()
            with db_path_flock(db_path):
                revert_result = revert_supersede(
                    db_path=db_path,
                    note_id=note_id,
                    target_note_id=title_slug or None,
                    rationale=rationale,
                )
            return str(revert_result)
        else:
            return _err(
                ErrorCode.INVALID_PARAMS,
                f"Unknown action '{action}'. Use: read, update, delete, restore, supersede, patch, revert_supersede",
            )
    except Exception as e:
        logger.exception("in memory_note verb")
        return _wrap_db_error("memory_note", e)


@mcp.tool()
@with_audit("memory_learn")
def memory_learn(
    content: str,
    as_skill: bool = False,
    skill_name: str = "",
    category: str = "lessons",
    tags: list[str] | None = None,
) -> str:
    """Save a lesson or compile a skill from content.

    Auto-categorizes and tags the memory. Optionally compiles a skill.

    Args:
        content: The lesson/skill content.
        as_skill: If True, compile as a skill (default False).
        skill_name: Skill directory name (required if as_skill=True).
        category: Target category (default: lessons).
        tags: Additional tags.
    """
    auth_err = _check_authorization("write", "memory")
    if auth_err:
        return auth_err
    try:
        from config import get_config
        from infra._lazy_imports import save_memory_journal, save_memory
        from save_pipeline import SaveValidationError
        from mcp_maintenance import memory_compile_skill

        cfg = get_config()
        _save_fn = save_memory_journal if cfg.write_journal else save_memory

        # Save the memory
        try:
            result = _save_fn(
                content=content,
                category=category,
                title_slug=skill_name or "",
                tags=tags or ["learned"],
                importance=4,
            )
        except SaveValidationError as e:
            return str(e)
        if as_skill and skill_name:
            skill_result = memory_compile_skill(
                lesson_slug=skill_name,
                skill_name=skill_name,
                primary_triggers=["learned"],
            )
            return f"Saved + compiled skill: {skill_name}\n{skill_result}"
        return str(result)
    except Exception as e:
        logger.exception("in memory_learn verb")
        return _wrap_db_error("memory_learn", e)


@mcp.tool()
@with_audit("memory_audit")
def memory_audit(
    hours: int = 24,
    limit: int = 20,
    include_errors: bool = True,
) -> str:
    """Review recent memory activity, errors, and system health.

    Combines audit_query + circuit_breaker_status into one call.

    Args:
        hours: Look back window (default 24h).
        limit: Max results (default 20).
        include_errors: Include error entries (default True).
    """
    auth_err = _check_authorization("search", "memory")
    if auth_err:
        return auth_err
    try:
        from mcp_audit import memory_audit_query, memory_circuit_breaker_status

        since_ts = None
        if hours > 0:
            import time

            since_ts = time.time() - (hours * 3600)

        audit = memory_audit_query(
            since_ts=since_ts,
            only_errors=not include_errors,
            limit=limit,
        )
        cb = memory_circuit_breaker_status(limit=5)
        return f"## Recent Activity\n{audit}\n\n## Circuit Breaker\n{cb}"
    except Exception as e:
        logger.exception("in memory_audit verb")
        return _wrap_db_error("memory_audit", e)


@mcp.tool()
@with_audit("memory_organize")
def memory_organize(
    target: str = "safe_default",
    dry_run: bool = False,
    confirm: bool = False,
) -> str:
    """Run safe memory maintenance batch.

    Targets:
      safe_default: compact + consolidate + rewrite_links
      full: safe_default + backfill + dedup + purge_expired
      compact: FTS5 compact only
      dedup: KG entity dedup only

    Args:
        target: Which batch to run (default: safe_default).
        dry_run: Preview without changes (default False).
        confirm: Required when target='full' and dry_run=False (purge is destructive).
    """
    auth_err = _check_authorization("admin", "maintenance")
    if auth_err:
        return auth_err
    try:
        from mcp_rebuild import memory_compact, memory_backfill_all
        from mcp_memory import memory_purge_expired
        from mcp_maintenance import (
            memory_consolidate,
            memory_rewrite_links,
            memory_dedup,
        )

        if target == "safe_default":
            results = [
                ("compact", memory_compact(dry_run=dry_run)),
                ("consolidate", memory_consolidate()),
                ("rewrite_links", memory_rewrite_links()),
            ]
        elif target == "full":
            if not dry_run and not confirm:
                return _err(
                    ErrorCode.INVALID_PARAMS,
                    "target='full' with dry_run=False triggers purge_expired "
                    "(destructive). Pass confirm=True to proceed.",
                )
            results = [
                ("compact", memory_compact(dry_run=dry_run)),
                ("consolidate", memory_consolidate()),
                ("rewrite_links", memory_rewrite_links()),
                ("backfill", memory_backfill_all(mode="health")),
                ("purge_expired", memory_purge_expired(dry_run=dry_run)),
                ("dedup", memory_dedup(action="duplicates", threshold=0.85)),
            ]
        elif target == "compact":
            return str(memory_compact(dry_run=dry_run))
        elif target == "dedup":
            return str(memory_dedup(action="duplicates", threshold=0.85))
        else:
            return _err(
                ErrorCode.INVALID_PARAMS,
                f"Unknown target '{target}'. Use: safe_default, full, compact, dedup",
            )

        lines = [f"# memory_organize({target}){' [DRY RUN]' if dry_run else ''}", ""]
        for name, result in results:
            lines.append(f"## {name}")
            lines.append(str(result))
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        logger.exception("in memory_organize verb")
        return _wrap_db_error("memory_organize", e)


@mcp.tool()
@with_audit("memory_share")
def memory_share(
    note_id: str,
    share_with: str = "",
    action: str = "list",
) -> str:
    """Share memories with other agents or view shared pool.

    Args:
        note_id: Memory to share (required for action=share).
        share_with: Target agent ID (for action=share).
        action: "list" | "share" | "import" | "stats".
    """
    auth_err = _check_authorization("share", "memory")
    if auth_err:
        return auth_err
    try:
        from mcp_sharing import (
            memory_shared_list,
            memory_share as _share_to_pool,
            memory_shared_import,
            memory_shared_stats,
        )

        if action == "list":
            try:
                from agent_context import get_agent as _share_list_agent
                _current_agent = _share_list_agent().agent_id
            except (ImportError, Exception):
                _current_agent = "default"
            return str(memory_shared_list(agent_id=_current_agent, category="", limit=50))
        elif action == "share":
            if not share_with:
                return _err(ErrorCode.INVALID_PARAMS, "share_with required for action=share")
            try:
                from agent_context import get_agent as _share_action_agent
                _sharer = _share_action_agent().agent_id
            except (ImportError, Exception):
                _sharer = "default"
            return str(_share_to_pool(
                note_id=note_id,
                agent_id=_sharer,
                target_agent_id=share_with,
                shared_with=share_with,
            ))
        elif action == "import":
            return str(memory_shared_import(shared_id=note_id, target_agent_id=share_with))
        elif action == "stats":
            return str(memory_shared_stats())
        else:
            return _err(ErrorCode.INVALID_PARAMS, f"Unknown action '{action}'")
    except Exception as e:
        logger.exception("in memory_share verb")
        return _wrap_db_error("memory_share", e)


@mcp.tool()
@with_audit("memory_graph")
def memory_graph(
    query: str = "",
    start: str = "",
    edge_patterns: str = "",
    max_depth: int = 2,
    action: str = "explore",
) -> str:
    """Explore the knowledge graph.

    Args:
        query: Natural language KG query (for action=explore).
        start: Starting entity/node ID (for action=traverse).
        edge_patterns: Edge type filter (for action=traverse).
        max_depth: Max traversal depth (default 2).
        action: "explore" | "traverse" | "shortest_path" | "stats".
    """
    auth_err = _check_authorization("search", "memory")
    if auth_err:
        return auth_err
    try:
        from mcp_kg import memory_facts_list, memory_graph_stats
        from mcp_kg_traversal import memory_graph_shortest_path, memory_graph_traverse

        if action == "explore":
            facts = memory_facts_list(facts_limit=20)
            stats = memory_graph_stats()
            return f"## KG Facts\n{facts}\n\n## Stats\n{stats}"
        elif action == "traverse":
            return str(memory_graph_traverse(start=start, edge_patterns=edge_patterns))
        elif action == "shortest_path":
            return str(memory_graph_shortest_path(source=start, target=edge_patterns, max_depth=max_depth))
        elif action == "stats":
            return str(memory_graph_stats())
        else:
            return _err(ErrorCode.INVALID_PARAMS, f"Unknown action '{action}'")
    except Exception as e:
        logger.exception("in memory_graph verb")
        return _wrap_db_error("memory_graph", e)


@mcp.tool()
@with_audit("memory_profile")
def memory_profile(
    action: str = "stats",
    agent_id: str = "",
) -> str:
    """View user profile, agent scopes, ARC stats, and cached skills.

    Args:
        action: "stats" | "user" | "agents" | "skills" | "arc".
        agent_id: Agent ID (for action=agents).
    """
    auth_err = _check_authorization("search", "memory")
    if auth_err:
        return auth_err
    try:
        from mcp_profile import memory_profile_stats, memory_user_profile
        from mcp_agent import memory_agent_list, memory_agent_init
        from mcp_maintenance import (
            memory_arc_stats,
            memory_list_skills,
        )

        if action == "stats":
            return str(memory_profile_stats())
        elif action == "user":
            return str(memory_user_profile())
        elif action == "agents":
            if agent_id:
                return str(memory_agent_init(agent_id=agent_id))
            return str(memory_agent_list())
        elif action == "skills":
            return str(memory_list_skills(limit=50))
        elif action == "arc":
            return str(memory_arc_stats())
        else:
            return _err(ErrorCode.INVALID_PARAMS, f"Unknown action '{action}'")
    except Exception as e:
        logger.exception("in memory_profile verb")
        return _wrap_db_error("memory_profile", e)


@mcp.tool()
@with_audit("memory_session_start")
def memory_session_start(query: str = "") -> str:
    """Retrieve the session startup briefing.

    Args:
        query: Optional topic to scope the briefing to.
    """
    auth_err = _check_authorization("search", "memory")
    if auth_err:
        return auth_err
    try:
        from mcp_search import memory_session_start as _session_start

        return str(_session_start(query=query))
    except Exception as e:
        logger.exception("in memory_session_start verb")
        return _wrap_db_error("memory_session_start", e)


@mcp.tool()
@with_audit("memory_advanced")
def memory_advanced(operation: str, tenant_id: str | None = None, **kwargs: str) -> str:
    """Power user escape hatch — pass through to any memory_maintenance operation.

    Use this when a verb doesn't cover your use case.

    Args:
        operation: Any memory_maintenance operation name.
        tenant_id: Tenant identity for tenant-scoped operations.
        **kwargs: Operation-specific parameters.

    Security: this delegates to ``memory_maintenance``, so the confirmation
    gate on destructive operations applies here too. A destructive op
    (e.g. ``purge_expired``, ``okf_export``, ``crdt_sync``) called without
    ``confirm=True`` is refused; pass ``confirm=True`` to proceed.
    """
    auth_err = _check_authorization("admin", "maintenance")
    if auth_err:
        return auth_err
    try:
        from mcp_maintenance import memory_maintenance

        if tenant_id is not None:
            kwargs["tenant_id"] = tenant_id
        return str(memory_maintenance(operation=operation, **kwargs))
    except Exception as e:
        logger.exception("in memory_advanced verb")
        return _wrap_db_error("memory_advanced", e)
