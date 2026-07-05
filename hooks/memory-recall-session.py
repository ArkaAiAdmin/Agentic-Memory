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
    if result:
        print(result)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"recall failed: {e}", file=sys.stderr)
        sys.exit(0)
