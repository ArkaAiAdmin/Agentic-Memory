"""MCP tools for session memory (schema v22).

CORE tools: memory_thread_context, memory_list_threads, memory_resolve_thread.
These are explicit retrieval tools — they do not modify search_memories behavior.
"""
from __future__ import annotations

from mcp_common import _bootstrap_path  # noqa: E402

import json
import logging
import os
from pathlib import Path

from mcp_instance import mcp
from mcp_common import _err, ErrorCode, with_audit

logger = logging.getLogger(__name__)


def _session_manager():
    from session_manager import SessionManager

    db_path = os.environ.get("MEMORY_DB_PATH")
    if db_path:
        return SessionManager(db_path=Path(db_path))
    return SessionManager()


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


@mcp.tool()
@with_audit("memory_thread_context")
def memory_thread_context(
    session_id: str = "",
    thread_id: str = "",
    include_events: bool = True,
    event_limit: int = 10,
) -> str:
    """Return active decision threads and recent events for a session."""
    try:
        mgr = _session_manager()
        if not session_id:
            try:
                from memory_common import get_memory_paths

                _, local_mem, _ = get_memory_paths()
                state_file = local_mem / "sessions" / ".current_session.json"
                if state_file.exists():
                    cs = json.loads(state_file.read_text())
                    session_id = cs.get("session_id", "")
            except Exception:
                pass
        if not session_id:
            return _err(ErrorCode.INVALID_PARAMS, "session_id is required")

        threads = mgr._load_open_threads(session_id)
        if thread_id:
            threads = [t for t in threads if t.id == thread_id]

        events_by_thread: dict = {}
        if include_events:
            raw = mgr._load_recent_events(session_id, per_thread=event_limit)
            for tid, evts in raw.items():
                if tid in [t.id for t in threads]:
                    events_by_thread[tid] = [
                        {
                            "id": e.id,
                            "seq": e.seq,
                            "event_type": e.event_type,
                            "content_summary": e.content_summary,
                            "memory_id": e.memory_id,
                            "confidence": e.confidence,
                            "created_at": e.created_at,
                        }
                        for e in evts[:event_limit]
                    ]

        result = {
            "session_id": session_id,
            "threads": [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status,
                    "created_at": t.created_at,
                    "events": events_by_thread.get(t.id, []),
                }
                for t in threads
            ],
        }
        return _json(result)
    except Exception as e:
        logger.error("memory_thread_context: %s", e)
        return _err(ErrorCode.DB_ERROR, str(e))


@mcp.tool()
@with_audit("memory_list_threads")
def memory_list_threads(
    session_id: str = "",
    limit: int = 20,
) -> str:
    """List decision threads for a session."""
    try:
        mgr = _session_manager()
        if not session_id:
            try:
                from memory_common import get_memory_paths

                _, local_mem, _ = get_memory_paths()
                state_file = local_mem / "sessions" / ".current_session.json"
                if state_file.exists():
                    cs = json.loads(state_file.read_text())
                    session_id = cs.get("session_id", "")
            except Exception:
                pass
        if not session_id:
            return _err(ErrorCode.INVALID_PARAMS, "session_id is required")

        threads = mgr._load_open_threads(session_id)
        threads = threads[:limit]

        result = {
            "session_id": session_id,
            "threads": [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status,
                    "created_at": t.created_at,
                }
                for t in threads
            ],
        }
        return _json(result)
    except Exception as e:
        logger.error("memory_list_threads: %s", e)
        return _err(ErrorCode.DB_ERROR, str(e))


@mcp.tool()
@with_audit("memory_resolve_thread")
def memory_resolve_thread(
    thread_id: str,
    resolution: str = "",
    superseded_by: str = "",
) -> str:
    """Resolve or close a decision thread."""
    try:
        if not thread_id:
            return _err(ErrorCode.INVALID_PARAMS, "thread_id is required")
        mgr = _session_manager()
        ok = mgr.resolve_thread(
            thread_id=thread_id,
            resolution=resolution,
            superseded_by=superseded_by or None,
        )
        return _json({"ok": ok, "thread_id": thread_id, "resolution": resolution})
    except Exception as e:
        logger.error("memory_resolve_thread: %s", e)
        return _err(ErrorCode.DB_ERROR, str(e))
