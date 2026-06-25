#!/usr/bin/env python3
"""PostToolUse / Stop hook: auto-save session memory if not already saved.

This hook makes Rule #7 (save session memory before ending) automatic.
It tracks whether a session-end save has happened in the current session
and auto-saves one if the agent forgets.

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

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _bootstrap_path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _log_error import log_error
except Exception:

    def log_error(exc, context=""):  # type: ignore[misc]
        import sys as _sys

        print(f"logger error: {exc} context={context}", file=_sys.stderr)


from memory_config import GLOBAL_MEM_DIR

_MARKER_FILE = GLOBAL_MEM_DIR / ".last_session_save.json"
_SESSION_SAVE_CATEGORY = "sessions"
_TOOL_THRESHOLD = 5  # auto-save if >= this many tool calls without a save


def _load_marker() -> dict:
    if _MARKER_FILE.exists():
        try:
            return json.loads(_MARKER_FILE.read_text())
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
            category=_SESSION_SAVE_CATEGORY,
            title_slug=f"auto-session-end-{session_id[:8] if session_id else 'unknown'}",
            tags=["auto-session-end", "rule-7"],
            importance=2,
        )
        # Mark as saved
        marker["saved_at"] = now
        _save_marker(marker)
        return {"saved": True, "reason": "auto_saved", "result": str(result)}
    except Exception as e:
        log_error(e, context="memory-session-end.auto_save")
        return {"saved": False, "reason": f"auto_save_failed: {e}"}


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
            # Stop event: check if we need to auto-save
            result = _maybe_auto_save()
            # Phase 5: pre-stop compliance gate — emit compliance score
            # as a non-blocking hint for the agent (Rule #7 enforcement
            # already covered above; this is the broader audit gate).
            compliance = _compliance_gate()
            if result.get("saved"):
                print(
                    json.dumps(
                        {"auto_saved": True, "detail": result, "compliance": compliance}
                    )
                )
            else:
                print(
                    json.dumps(
                        {
                            "auto_saved": False,
                            "reason": result.get("reason", ""),
                            "compliance": compliance,
                        }
                    )
                )
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
