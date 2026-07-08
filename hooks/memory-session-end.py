#!/usr/bin/env python3
"""PostToolUse / Stop hook: auto-save + auto-reinforce session memory.

This hook makes Rule #7 (save session memory before ending) automatic.
It tracks whether a session-end save has happened in the current session
and auto-saves one if the agent forgets.

Phase 6: auto-reinforce (P2) — after a productive session, queries
user_access_log for memories accessed and applies +0.5 delta to their
success_score, so frequently-retrieved memories get a boost.

Hook wiring (to be added in opencode hooks config):
  PostToolUse: post:memory-session-end
  Stop:        stop:memory-session-end

Both fire this same script. On PostToolUse, it just updates the marker.
On Stop, it performs the actual save if needed.

The marker file (memory/.last_session_save.json) tracks:
  - session_id: the current opencode session
  - saved_at: timestamp of last session save
  - tool_count: number of tool calls in this session

If the Stop event fires and saved_at is missing or older than the
session start, the hook auto-saves a session memory via the MCP tool.
"""

import logging

import fcntl
import json
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _log_error import log_error
except Exception:

    def log_error(exc, context=""):  # type: ignore[misc]
        import sys as _sys

        print(f"logger error: {exc} context={context}", file=_sys.stderr)


from infra.memory_config import GLOBAL_MEM_DIR

_MARKER_FILE = GLOBAL_MEM_DIR / ".last_session_save.json"
_SESSION_SAVE_CATEGORY = "sessions"
_TOOL_THRESHOLD = 5  # auto-save if >= this many tool calls without a save

SESSIONS_DIR = GLOBAL_MEM_DIR / "sessions"
_CURRENT_SESSION_FILE = SESSIONS_DIR / ".current_session.json"
_CURRENT_SESSION_LOCK = SESSIONS_DIR / ".current_session.json.flock"


def _read_current_session() -> dict:
    if not _CURRENT_SESSION_FILE.exists():
        return {}
    try:
        result: dict = json.loads(_CURRENT_SESSION_FILE.read_text())
        return result
    except (json.JSONDecodeError, OSError):
        return {}


def _clear_current_session() -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = _CURRENT_SESSION_LOCK.open("w")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _CURRENT_SESSION_FILE.write_text("{}")
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            fd.close()
    except OSError:
        _CURRENT_SESSION_FILE.write_text("{}")


def _end_session_via_manager(session_id: str, marker: dict) -> dict:
    """Call SessionManager.end_session() to close the session entity."""
    try:
        from session_manager import SessionManager

        mgr = SessionManager()

        # Build lightweight summary from marker — no LLM call
        tool_count = marker.get("tool_count", 0)
        summary = (
            f"Session {session_id[:12]} ended.\n"
            f"Tool calls: {tool_count}\n"
            f"Ended via memory-session-end hook (Rule #7 enforcement)."
        )
        result = mgr.end_session(session_id, summary=summary)
        _clear_current_session()
        return {
            "ended": True,
            "session_id": session_id,
            "summary_note_id": getattr(result, "summary_note_id", None)
            if result
            else None,
        }
    except Exception as e:
        logger.warning("_end_session_via_manager failed: %s", e)
        log_error(e, context="memory-session-end.end_session")
        return {"ended": False, "error": str(e)}


def _load_marker() -> dict:
    if _MARKER_FILE.exists():
        try:
            result: dict = json.loads(_MARKER_FILE.read_text())
            return result
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_marker(marker: dict) -> None:
    try:
        _MARKER_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MARKER_FILE.write_text(json.dumps(marker, indent=2))
    except OSError as exc:
        logger.debug("memory-session-end: cannot write marker file: %s", exc)


def _update_tool_count(tool_name: str = "") -> dict:
    """Record that a tool was called. Returns updated marker."""
    marker = _load_marker()
    marker.setdefault("session_id", "")
    marker.setdefault("saved_at", 0)
    marker.setdefault("tool_count", 0)
    marker["tool_count"] = marker.get("tool_count", 0) + 1
    marker.setdefault("first_tool_at", marker.get("first_tool_at", time.time()))
    _save_marker(marker)
    return marker


def _try_promote_session_drafts() -> None:
    """Phase 3: lightweight per-session promotion scan.

    Scans ``memories`` for auto-capture drafts (lessons/, importance <= 2)
    that have at least one retrieval event in this session's time window
    (last 2 h).  Promotes qualifying notes to importance=4 with
    ``promoted`` + ``curated`` tags.

    Best-effort: swallowed errors never propagate to the caller so the
    session-end save is never blocked.
    """
    try:
        import sqlite3 as _sqlite3
        from pathlib import Path as _Path
        from datetime import datetime as _dt, timezone as _tz, timedelta as _tdelta

        sys_path = str(_Path(__file__).resolve().parent.parent)
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        from infra.infrastructure import resolve_active_memory_dir as _resolvedir

        db_path = _resolvedir() / "memory.db"
        if not db_path.exists():
            return

        conn = _sqlite3.connect(str(db_path), timeout=5)
        try:
            cutoff_iso = (_dt.now(_tz.utc) - _tdelta(hours=2)).isoformat()
            rows = conn.execute(
                "SELECT id FROM memories WHERE category='lessons' AND importance<=2 "
                "AND tags LIKE '%auto-capture%' AND tags NOT LIKE '%promoted%' "
                "AND created_at >= ? ORDER BY created_at DESC LIMIT 10",
                (cutoff_iso,),
            ).fetchall()
            promoted_any = False
            now_ts = _dt.now(_tz.utc).timestamp()
            for (nid,) in rows:
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM user_access_log WHERE note_id=? "
                    "AND access_ts >= ?",
                    (nid, now_ts - 7200),
                ).fetchone()[0]
                if cnt >= 1:
                    now_iso = _dt.now(_tz.utc).isoformat()
                    conn.execute(
                        "UPDATE memories SET importance=4, tags = "
                        "json_insert(COALESCE(tags,'[]'), '$[#]', 'promoted'), "
                        "metadata = json_set(COALESCE(metadata,'{}'), '$.promoted_at', ?), "
                        "updated_at=? WHERE id=?",
                        (now_iso, now_iso, nid),
                    )
                    promoted_any = True
            if promoted_any:
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("_try_promote_session_drafts failed: %s", e)


def _maybe_auto_save() -> dict:
    """If no session save has happened recently, auto-save one.

    Returns a result dict with 'saved' (bool) and 'reason' (str).
    """
    marker = _load_marker()
    now = time.time()
    session_id = marker.get("session_id", "")
    saved_at = marker.get("saved_at", 0)
    tool_count = marker.get("tool_count", 0)

    # Already saved in this session — nothing to do
    if saved_at > 0 and tool_count > 0:
        return {"saved": False, "reason": "already_saved_this_session"}

    # Not enough activity to warrant a save
    if tool_count < _TOOL_THRESHOLD:
        return {"saved": False, "reason": f"insufficient_activity_{tool_count}_tools"}

    # Try to auto-save via the MCP tool
    try:
        # We import here to avoid startup cost if not needed
        from mcp_memory import memory_save

        # Phase 3: lightweight promotion scan for auto-capture drafts
        # from the current session. Runs best-effort; never blocks the
        # session-end save.
        _try_promote_session_drafts()

        # Build a summary of the session from available context
        tool_count_str = str(tool_count)
        content = (
            f"Auto-saved session end marker.\n"
            f"Session ID: {session_id}\n"
            f"Tool calls: {tool_count_str}\n"
            f"Auto-saved by memory-session-end hook (Rule #7 enforcement).\n"
        )

        result = memory_save(
            content=content,
            category="sessions",
            title_slug=f"auto-session-end-{session_id[:8] if session_id else 'unknown'}",
            tags=["auto-session-end", "rule-7"],
            importance=2,
        )
        # Mark as saved
        marker["saved_at"] = now
        _save_marker(marker)
        return {"saved": True, "reason": "auto_saved", "result": str(result)}
    except Exception as e:
        logger.warning("_maybe_auto_save failed: %s", e)
        log_error(e, context="memory-session-end.auto_save")
        return {"saved": False, "reason": f"auto_save_failed: {e}"}


def _collect_session_memory_ids(marker: dict) -> list[str]:
    """Query user_access_log for memory IDs accessed since first_tool_at.

    Returns the list of distinct note_id values accessed in this session.
    Best-effort: returns empty list on any error.
    """
    first_tool_at = marker.get("first_tool_at")
    if not first_tool_at:
        return []
    try:
        from infra.infrastructure import resolve_active_memory_dir
        import sqlite3

        db_path = resolve_active_memory_dir() / "memory.db"
        if not db_path.exists():
            return []
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            rows = conn.execute(
                "SELECT DISTINCT note_id FROM user_access_log "
                "WHERE access_ts >= ? ORDER BY note_id",
                (first_tool_at,),
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()
    except Exception as e:
        logger.warning("_collect_session_memory_ids failed: %s", e)
        return []


def _auto_reinforce(marker: dict) -> dict:
    """Apply outcome feedback: reinforce memories accessed this session.

    Success signal: auto-save completed or was already saved this session
    (meaning the session had enough activity to be productive).
    Delta: +0.5 for productive sessions (gentle boost).
    Best-effort: never blocks the session end.
    """
    saved_at = marker.get("saved_at", 0)
    tool_count = marker.get("tool_count", 0)
    if not saved_at or tool_count < _TOOL_THRESHOLD:
        return {"reinforced": False, "reason": "insufficient_activity"}
    memory_ids = _collect_session_memory_ids(marker)
    if not memory_ids:
        return {"reinforced": False, "reason": "no_memories_accessed"}
    try:
        sys_path = str(Path(__file__).resolve().parent.parent)
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        from save.pipeline import reinforce_memories_db
        from infra.infrastructure import resolve_active_memory_dir

        db_path = resolve_active_memory_dir() / "memory.db"
        updated = reinforce_memories_db(db_path, memory_ids, delta=0.5)
        return {"reinforced": True, "updated_count": updated, "session_ids": len(memory_ids)}
    except Exception as e:
        logger.warning("_auto_reinforce failed: %s", e)
        log_error(e, context="memory-session-end._auto_reinforce")
        return {"reinforced": False, "reason": f"error: {e}"}


def _compliance_gate() -> dict:
    """Phase 5 pre-stop: lightweight compliance score on session end.

    Returns a small dict with the compliance score.
    Best-effort: never blocks session end.
    """
    try:
        sys_path = str(Path(__file__).resolve().parent.parent)
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        from mcp_maintenance import memory_maintenance

        result = memory_maintenance(operation="compliance_check", kwargs={})
        if isinstance(result, dict):
            return {"score": result.get("score"), "issues": result.get("issues", [])}
        return {}
    except Exception as _e:
        logger.warning("_compliance_gate failed: %s", _e)
        return {"error": str(_e)}


def main():
    try:
        raw = sys.stdin.read()
        hook_data = {}
        if raw.strip():
            try:
                hook_data = json.loads(raw)
            except json.JSONDecodeError:
                pass

        tool_name = hook_data.get("tool_name", "")
        session_id = hook_data.get("session_id", "")

        # Update marker with session_id if provided
        marker = _load_marker()
        if session_id and marker.get("session_id") != session_id:
            marker["session_id"] = session_id
            marker["saved_at"] = 0  # new session, not yet saved
            marker["tool_count"] = 0
            _save_marker(marker)

        # Determine hook type from environment (PostToolUse vs Stop)
        hook_event = os.environ.get("MEMORY_HOOK_EVENT", "")

        if hook_event == "stop" or not tool_name:
            # Stop event: end session entity via SessionManager
            session_end_result = {}
            cs = _read_current_session()
            entity_session_id = cs.get("session_id", "")
            if entity_session_id:
                session_end_result = _end_session_via_manager(entity_session_id, marker)

            # Auto-save session memory if not already saved
            result = _maybe_auto_save()
            # Phase 5: pre-stop compliance gate
            compliance = _compliance_gate()
            # Phase 6: reinforce memories accessed this productive session
            reinforce_result = _auto_reinforce(marker)
            combined = {
                "session_end": session_end_result,
                "auto_saved": result.get("saved", False),
                "reason": result.get("reason", ""),
                "compliance": compliance,
                "reinforce": reinforce_result,
            }
            if result.get("saved"):
                combined["detail"] = result
            print(json.dumps(combined))
        else:
            # PostToolUse (or any other event): just update tool count
            marker = _update_tool_count(tool_name)
            # If we hit the threshold, proactively save
            if (
                marker.get("tool_count", 0) >= _TOOL_THRESHOLD
                and marker.get("saved_at", 0) == 0
            ):
                result = _maybe_auto_save()
                if result.get("saved"):
                    print(json.dumps({"auto_saved": True, "detail": result}))
            else:
                print(json.dumps({"tool_count": marker.get("tool_count", 0)}))

    except Exception as e:
        logger.warning("main failed: %s", e)
        log_error(e, context="memory-session-end.main()")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as _hook_e:
        try:
            log_error(_hook_e, context="memory-session-end.top_level")
        except Exception:
            import sys as _sys

            print(f"hook fatal: {_hook_e}", file=_sys.stderr)
        import sys as _sys

        _sys.exit(0)
