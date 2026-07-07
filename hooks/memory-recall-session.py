#!/usr/bin/env python3
"""
Session recap — called from ecc-hooks.ts session.created handler.

Receives the session.created event JSON on stdin, extracts a searchable
query from it (prompt / task / cwd), and passes it to session_recap()
so that the 4-tier recall policy can do contextual retrieval instead of
returning raw session transcript noise.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import infra._bootstrap_path  # noqa: E402, F401

from recall.recall import session_recap  # noqa: E402

_MARKER_FILE = Path(__file__).resolve().parent.parent / "memory" / ".last_session_save.json"


def _load_last_session_marker() -> dict[str, object]:
    """Read the last session marker for session-end timestamp."""
    if not _MARKER_FILE.exists():
        return {}
    try:
        raw: dict[str, object] = json.loads(_MARKER_FILE.read_text())
        return raw
    except (json.JSONDecodeError, OSError):
        return {}


def _fetch_recent_contradictions(since_ts: float | None) -> str:
    """Return a formatted block of recent contradiction events.

    Uses a direct SQL query against kg_facts for supersession events
    where transaction_time >= since_ts (or all if None).
    Returns an empty string if none found.
    """
    if since_ts is None:
        return ""
    try:
        from infra.infrastructure import resolve_active_memory_dir
        import sqlite3

        db_path = resolve_active_memory_dir() / "memory.db"
        if not db_path.exists():
            return ""
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            rows = conn.execute(
                "SELECT f1.id, f1.subject, f1.predicate, f1.object, "
                "f2.subject, f2.predicate, f2.object, "
                "f1.invalidation_reason, f1.contradiction_score "
                "FROM kg_facts f1 "
                "JOIN kg_facts f2 ON f1.superseded_by = f2.id "
                "WHERE f1.transaction_time >= ? "
                "ORDER BY f1.transaction_time DESC LIMIT 10",
                (since_ts,),
            ).fetchall()
            if not rows:
                return ""
            lines = []
            lines.append("\n## Recent Contradictions")
            lines.append(
                "The following facts were superseded since your last session. "
                "Review and resolve if needed:"
            )
            for (
                old_id, old_subj, old_pred, old_obj,
                new_subj, new_pred, new_obj,
                reason, score,
            ) in rows:
                reason_str = reason or "contradicted"
                score_str = f" (score={score:.2f})" if score else ""
                lines.append(
                    f"- `{old_subj}` {old_pred} `{old_obj}` → "
                    f"`{new_subj}` {new_pred} `{new_obj}`"
                    f" [{reason_str}{score_str}]"
                )
            return "\n".join(lines)
        finally:
            conn.close()
    except Exception:
        return ""


def _extract_session_query(hook_data: dict) -> str:
    """Extract a searchable query from session start hook data.

    Mirrors the same logic in memory-session-start.py extract_session_query().
    """
    for field in ["task", "prompt", "query", "description", "message", "goal"]:
        val = hook_data.get(field, "")
        if isinstance(val, str) and len(val) > 10:
            return val[:300]

    tool_input = hook_data.get("tool_input", {})
    if isinstance(tool_input, dict):
        for field in ["task", "prompt", "query", "description"]:
            val = tool_input.get(field, "")
            if isinstance(val, str) and len(val) > 10:
                return val[:300]

    cwd = (
        hook_data.get("cwd")
        or hook_data.get("project_dir")
        or hook_data.get("workspace")
        or ""
    )
    if isinstance(cwd, str) and len(cwd) > 3:
        return f"project context for {cwd}"[:300]

    return ""


def main():
    raw = sys.stdin.read()
    hook_data = {}
    if raw.strip():
        try:
            hook_data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            pass

    query = _extract_session_query(hook_data)
    result = session_recap(query=query)
    if not result:
        result = ""

    # Append recent contradictions from the last session's end
    marker = _load_last_session_marker()
    raw_ts: object = marker.get("saved_at") or marker.get("first_tool_at")
    last_session_end: float | None = None
    if isinstance(raw_ts, (int, float)):
        last_session_end = float(raw_ts)
    contradictions = _fetch_recent_contradictions(last_session_end)
    if contradictions:
        result += contradictions

    if result.strip():
        print(result)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"recall failed: {e}", file=sys.stderr)
        sys.exit(0)
