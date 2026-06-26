#!/usr/bin/env python3
"""
SessionStart hook: auto-load memory context at session start.

Two-phase push:
1. Bootstrap: pinned notes, high-importance, recent notes
2. Proactive push: search for memories relevant to the session's
   initial context (task description, project path, etc.)

The push phase extracts a query from the hook input and searches
memory BEFORE the agent starts working, so relevant context is
available from the first tool call.

I5 fix (2026-06-22): the PYTHON resolution now tries multiple
venv locations (``.venv``, ``venv``, ``~/.venv``) and falls back
to ``sys.executable`` when the hook is run via ``python -m`` or
similar. The previous hardcoded single-path check broke for
``.venv``-named environments.

I10 fix (2026-06-22): ``search_memories`` is imported directly
from ``search.orchestrator`` (the canonical source) instead of
going through the ``search_pipeline`` re-export shim.

I1 fix (2026-06-22): participates in the per-process search
dedup cache shared with the other two hooks.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _bootstrap_path  # noqa: E402

import json
import subprocess

# Make the sibling _log_error.py importable (same dir as this hook)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _log_error import log_error  # noqa: E402

# I5 fix: try multiple venv locations. The old single-path check
# only matched ``<INSTALL_ROOT>/venv/bin/python`` and silently
# broke for ``.venv``, ``~/.venv``, or ``/opt/homebrew/bin/python``
# installations.
_HOOK_DIR = Path(__file__).resolve().parent
_INSTALL_ROOT = _bootstrap_path.INSTALL_ROOT


def _resolve_venv_python() -> str:
    """Pick the venv python that actually exists on this system.

    Order: ``MEMORY_PYTHON`` env var → ``sys.executable`` if it
    looks like a venv interpreter → known venv bin names under the
    install root → ``sys.executable`` as last-resort fallback.
    """
    override = os.environ.get("MEMORY_PYTHON")
    if override and Path(override).exists():
        return override

    # If we're already inside a venv (sys.executable is under a
    # venv dir), prefer it so nested launches stay inside the
    # same env.
    exe_path = Path(sys.executable)
    exe_str = str(exe_path)
    looks_like_venv = (
        "venv" in exe_str.split("/")
        or exe_path.parent.name == "bin"
        and exe_path.parent.parent.name in (".venv", "venv")
    )
    if looks_like_venv and exe_path.exists():
        return exe_str

    # Try the well-known venv bin names under the install root.
    for venv_dir in (_INSTALL_ROOT / "venv", _INSTALL_ROOT / ".venv"):
        candidate = venv_dir / "bin" / "python"
        if candidate.exists():
            return str(candidate)

    # Last resort: whatever Python is running us right now. The
    # subprocess call will use the same interpreter.
    return exe_str


PYTHON = _resolve_venv_python()
BOOTSTRAP = str(_HOOK_DIR.parent / "memory_bootstrap.py")

# Lazy imports — only loaded if we do proactive search
_search_fn = None
_paths_fn = None


def _load_search():
    global _search_fn, _paths_fn
    if _search_fn is None:
        # I10 fix: import directly from search.orchestrator.
        from search.orchestrator import search_memories  # noqa: E402
        from memory_common import get_memory_paths  # noqa: E402

        _search_fn = search_memories
        _paths_fn = get_memory_paths


# I1 fix: shared per-process dedup cache.
_SEARCH_CACHE: dict[str, tuple[float, list[dict]]] = {}
_SEARCH_CACHE_MAX = int(os.environ.get("MEMORY_HOOK_CACHE_SIZE", "5"))
_SEARCH_CACHE_TTL = float(os.environ.get("MEMORY_HOOK_CACHE_TTL", "300"))


def _cache_get(query: str) -> list[dict] | None:
    entry = _SEARCH_CACHE.get(query)
    if entry is None:
        return None
    ts, results = entry
    if (time.time() - ts) > _SEARCH_CACHE_TTL:
        _SEARCH_CACHE.pop(query, None)
        return None
    return results


def _cache_put(query: str, results: list[dict]) -> None:
    _SEARCH_CACHE[query] = (time.time(), results)
    while len(_SEARCH_CACHE) > _SEARCH_CACHE_MAX:
        oldest = min(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])
        _SEARCH_CACHE.pop(oldest, None)


def extract_session_query(hook_data: dict) -> str:
    """Extract a searchable query from session start hook data.

    Falls back to recent file paths and project name when no explicit
    task/prompt field exists in the hook data.
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

    # Fallback: use project path or CWD as context signal
    cwd = (
        hook_data.get("cwd")
        or hook_data.get("project_dir")
        or hook_data.get("workspace")
        or ""
    )
    if isinstance(cwd, str) and len(cwd) > 3:
        return f"project context for {cwd}"[:300]

    return ""


def proactive_search(query: str, db_path: Path | None = None) -> str:
    """Search memory for relevant context based on session query."""
    try:
        _load_search()
        if _search_fn is None or _paths_fn is None:
            return ""
        if db_path is None:
            _, local_mem, _ = _paths_fn()
            db_path = local_mem / "memory.db"
        if db_path is None or not db_path.exists():
            return ""

        # I1 dedup: skip if a sibling hook just ran this query.
        cached = _cache_get(query)
        if cached is not None:
            items = cached
        else:
            # I7 fix: read limit from MEMORY_HOOK_RESULT_LIMIT
            # (default 5, matching the previous hardcoded value).
            try:
                limit = int(os.environ.get("MEMORY_HOOK_RESULT_LIMIT", "5"))
            except ValueError:
                limit = 5
            results = _search_fn(
                db_path=db_path,
                query=query,
                limit=limit,
                include_global=True,
            )
            items = results.get("results", [])
            if not isinstance(items, list):
                items = []
            _cache_put(query, items)

        if not items:
            return ""

        lines = ["\n## Proactive Memory Suggestions"]
        for i, r in enumerate(items, 1):
            content = r.get("content", "")[:200]
            score = r.get("final_score", 0)
            mid = r.get("id", "unknown")
            lines.append(f"[{i}] **{mid}** (relevance: {score:.2f})")
            lines.append(f"    {content}...")
        return "\n".join(lines)
    except Exception as e:
        log_error(e, context="memory-session-start.proactive_search()")
        return ""


def main():
    try:
        raw = sys.stdin.read()
        hook_data = {}
        if raw.strip():
            try:
                hook_data = json.loads(raw)
            except json.JSONDecodeError:
                pass

        # Phase 1: Bootstrap (pinned, high-importance, recent)
        bootstrap_output = ""
        try:
            from memory_bootstrap import get_bootstrap_summary

            os.environ.setdefault("MEMORY_KNOWLEDGE_GRAPH", "1")
            bootstrap_output = get_bootstrap_summary()
        except Exception as e:
            log_error(e, context="memory-session-start.bootstrap_inprocess")

        # Phase 2: Proactive push (search based on session context)
        proactive_output = ""
        query = extract_session_query(hook_data)
        if query:
            proactive_output = proactive_search(query)

        # Phase 4 (2026-06-25): inject Rules #2 and #6 reminders
        # directly into the session-start briefing so the agent sees
        # them before designing features or editing write-path code.
        rules_reminder = (
            "\n\n## Reliability Rules\n"
            "- **Rule #2** — Before designing a new feature: search for "
            '"<feature> <subsystem> design rationale"\n'
            "- **Rule #6** — Before pushing write-path code: search for "
            '"save_pipeline saga transaction safety"\n'
        )

        # Combine outputs
        parts = []
        if bootstrap_output:
            parts.append(bootstrap_output)
        if proactive_output:
            parts.append(proactive_output)
        if rules_reminder.strip():
            parts.append(rules_reminder)

        if parts:
            print("\n\n".join(parts))

    except Exception as e:
        log_error(e, context="memory-session-start.main()")


if __name__ == "__main__":
    # 2026-06-19 resilience: same pattern as memory-proactive-context.py
    try:
        main()
    except SystemExit:
        raise
    except BaseException as _hook_e:  # noqa: BLE001
        try:
            log_error(_hook_e, context="memory-session-start.top_level")
        except Exception:
            import sys as _sys

            print(f"hook fatal: {_hook_e}", file=_sys.stderr)
        import sys as _sys

        _sys.exit(0)
