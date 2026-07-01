#!/usr/bin/env python3
"""
Context Monitor for Agentic Memory

Tracks tool call frequency and triggers memory checkpoints to prevent
context loss during long sessions and silent compaction events.

Called from OpenCode plugin hooks:
  - tool.execute.after  → track_tool_call (increment counter, periodic checkpoint)
  - session.idle        → session_idle (capture current state)
  - session.deleted     → session_end (final summary)
  - compacting          → pre_compaction (save before context is lost)
"""

import fcntl
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

# C15 fix (2026-06-27): prevent MPS kernel crash when concurrent subprocesses
# load sentence-transformers on Apple Silicon. Must be set before any torch import.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

logger = logging.getLogger(__name__)

MEMORY_DIR = Path(
    os.environ.get(
        "AGENTIC_MEMORY_DIR", Path.home() / ".config" / "agentic-memory" / "memory"
    )
)
SESSIONS_DIR = MEMORY_DIR / "sessions"
STATE_FILE = SESSIONS_DIR / ".context_monitor_state.json"

# Module-level keep-alive for the state lock FD. fcntl.flock releases
# when the FD is closed; a local variable in _save_state would go out
# of scope after the function returns between consecutive calls.
_STATE_LOCK_FD = None
_STATE_LOCK_PATH = SESSIONS_DIR / ".context_monitor_state.json.flock"

# Configuration
CHECKPOINT_INTERVAL = 10  # Save checkpoint every N tool calls
COMPACTION_THRESHOLD = 0.7  # Save summary when context estimated > 70% full


def _load_state() -> dict:
    """Load persistent state from disk.

    Preserves cumulative tool_call_count across sessions so that
    pre_compaction() knows the true session length.
    """
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            if isinstance(state, dict):
                # Preserve cumulative count across sessions
                if "total_tool_calls" not in state:
                    state["total_tool_calls"] = state.get("tool_call_count", 0)
                return state
        except Exception:
            logger.warning("Failed to load monitor state from %s", STATE_FILE)
            pass
    return {
        "tool_call_count": 0,
        "total_tool_calls": 0,
        "last_checkpoint_tool_count": 0,
        "last_checkpoint_time": time.time(),
        "session_start_time": time.time(),
        "tools_since_checkpoint": 0,
        "notable_tools": [],
        "last_compaction_time": 0.0,
    }


def _save_state(state: dict):
    """Persist state to disk.

    Uses an exclusive flock on a sibling .flock file so concurrent
    writers (track_tool_call, session_idle, pre_compaction) cannot
    clobber each other. The actual write uses atomic_write (temp + os.replace)
    so a crash mid-write never produces a truncated file.
    """
    global _STATE_LOCK_FD
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # Acquire exclusive flock; re-open FD each call so we don't hold
    # it between calls (the hook model is short-lived subprocesses,
    # not long-lived daemons).
    try:
        lock_fd = open(_STATE_LOCK_PATH, "w", encoding="utf-8")
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
    except OSError as exc:
        logger.debug("context_monitor: cannot acquire state lock: %s", exc)
        lock_fd = None  # best-effort: proceed without lock

    try:
        from memory_common import atomic_write

        atomic_write(STATE_FILE, json.dumps(state, indent=2) + "\n")
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                lock_fd.close()
            except OSError as exc:
                logger.debug("context_monitor: cannot release state lock: %s", exc)


def _today_str() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _now_str() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _extract_preview(tool_name: str, params_json: str, result_preview: str) -> str:
    """Extract a meaningful preview from tool params and result."""
    # Extract from params based on tool type (prefer over result_preview)
    try:
        params = json.loads(params_json) if params_json else {}
    except Exception:
        logger.warning("Failed to parse params JSON in _extract_preview")
        params = {}

    # Ensure params is a dict for .get() calls; handle string/None params
    if not isinstance(params, dict):
        if isinstance(params, str) and params:
            # params is a raw string (e.g. bash command passed directly)
            if tool_name == "bash":
                return f"$ {params[:120]}"
            return params[:150]
        params = {}

    if tool_name == "bash":
        cmd = params.get("command", "")
        if not cmd and isinstance(params_json, str):
            try:
                parsed = json.loads(params_json)
                if isinstance(parsed, str):
                    cmd = parsed
            except Exception:
                logger.warning("Failed to parse bash command from string params")
                pass
        if cmd:
            return f"$ {cmd[:120]}"
    elif tool_name in ("read", "view_file", "read_file", "view"):
        p = params.get("path", params.get("filePath", params.get("AbsolutePath", "")))
        if p:
            return f"read {p.split('/')[-1]}"
    elif tool_name in ("write", "write_file", "write_to_file"):
        p = params.get("path", params.get("filePath", params.get("TargetFile", "")))
        if p:
            content_preview = params.get("content", params.get("CodeContent", ""))[:80]
            return f"write {p.split('/')[-1]}: {content_preview}"
    elif tool_name in ("edit", "replace_file_content", "multi_replace_file_content"):
        p = params.get("path", params.get("filePath", params.get("TargetFile", "")))
        if p:
            return f"edit {p.split('/')[-1]}"
    elif tool_name in ("glob", "glob_files"):
        pattern = params.get("pattern", "")
        if pattern:
            return f"glob {pattern}"
    elif tool_name in ("grep", "grep_search"):
        pattern = params.get("pattern", params.get("Query", ""))
        if pattern:
            return f"grep {pattern}"
    elif tool_name == "run_command":
        cmd = params.get("CommandLine", "")
        if cmd:
            return f"run `{cmd[:120]}`"
    elif tool_name in ("search_web", "web_search"):
        query = params.get("query", "")
        if query:
            return f"web-search: {query[:100]}"
    elif tool_name == "memory_save":
        title = params.get("title_slug", "")
        return f"save {title}" if title else "memory_save"
    elif tool_name == "memory_search":
        query = params.get("query", "")
        return f"search: {query[:100]}" if query else "memory_search"
    elif tool_name == "memory_delete":
        note_id = params.get("note_id", "")
        return f"delete {note_id}" if note_id else "memory_delete"
    elif tool_name == "question":
        q = params.get("question", "")
        return f"question: {q[:100]}" if q else "question"
    elif tool_name == "todowrite":
        todos = params.get("todos", [])
        if todos:
            items = [t.get("content", "")[:40] for t in todos[:3] if t.get("content")]
            return f"todos: {', '.join(items)}"

    # Fallback to result preview if param extraction didn't produce anything
    if result_preview:
        return result_preview[:150]
    return ""


def track_tool_call(
    tool_name: str, params_json: str = "{}", result_preview: str = ""
) -> dict:
    """Track a tool call. Returns checkpoint info if checkpoint is due."""
    state = _load_state()

    state["tool_call_count"] += 1
    state["total_tool_calls"] = state.get("total_tool_calls", 0) + 1
    state["tools_since_checkpoint"] += 1
    state["last_checkpoint_time"] = time.time()

    # Extract meaningful preview from params
    preview = _extract_preview(tool_name, params_json, result_preview)

    # Track notable tools (not routine reads/searches, but including all important developer actions)
    notable = {
        # Direct action / developer tools — bare and namespaced forms
        "bash",
        "agentic-memory_bash",
        "write",
        "edit",
        "write_file",
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
        "run_command",
        "run-tests",
        "check-coverage",
        "security-audit",
        "format-code",
        "lint-check",
        "git-summary",
        "changed-files",
        "grep_search",
        "grep",
        "glob_files",
        "glob",
        "view_file",
        "read_file",
        "search_web",
        "web_search",
        "todowrite",
        "agentic-memory_todowrite",
        "task",
        "question",
        "agentic-memory_question",
        # Memory / KG tools (repo-native + Omega skill)
        "memory_save",
        "agentic-memory_memory_save",
        "memory_delete",
        "agentic-memory_memory_delete",
        "memory_reinforce",
        "memory_graph_search",
        "memory_graph_stats",
        "memory_graph_shortest_path",
        "memory_graph_traverse",
        "memory_temporal_query",
        "memory_temporal_contradictions",
        "memory_create_entities",
        "agentic-memory_memory_create_entities",
        "memory_add_observations",
        "agentic-memory_memory_add_observations",
        "memory_search_nodes",
        "agentic-memory_memory_search_nodes",
        "memory_open_nodes",
        "agentic-memory_memory_open_nodes",
        "memory_delete_entities",
        "agentic-memory_memory_delete_entities",
        "memory_delete_observations",
        "agentic-memory_memory_delete_observations",
        "memory_create_relations",
        "agentic-memory_memory_create_relations",
        "memory_delete_relations",
        "agentic-memory_memory_delete_relations",
    }

    def _tool_matches(name: str, candidates: set[str]) -> bool:
        if name in candidates:
            return True
        if "_" in name:
            unprefixed = name.split("_", 1)[1]
            if unprefixed in candidates:
                return True
        return False

    if _tool_matches(tool_name, notable) or state["tools_since_checkpoint"] <= 3:
        state["notable_tools"].append(
            {
                "tool": tool_name,
                "time": time.time(),
                "preview": preview[:150] if preview else "",
            }
        )
        # Keep only last 100 notable tools
        state["notable_tools"] = state["notable_tools"][-100:]

    _save_state(state)

    # Check if checkpoint is due
    checkpoint_due = state["tools_since_checkpoint"] >= CHECKPOINT_INTERVAL

    return {
        "checkpoint_due": checkpoint_due,
        "tool_call_count": state["tool_call_count"],
        "tools_since_checkpoint": state["tools_since_checkpoint"],
        "notable_tools": state["notable_tools"][-5:],  # Last 5 for context
    }


def session_idle(session_id: str = "", summary_hint: str = "") -> dict:
    """Called when agent stops responding (session.idle).

    Saves a lightweight progress snapshot. This captures the agent's
    current state without requiring a full summary.
    """
    state = _load_state()
    elapsed_min = (time.time() - state["session_start_time"]) / 60

    # Build progress note
    notable_summary = []
    for t in state["notable_tools"][-10:]:
        preview = t.get("preview", "")
        preview_str = f" → {preview}" if preview else ""
        notable_summary.append(
            f"- `{t['tool']}` at {time.strftime('%H:%M', time.localtime(t['time']))}{preview_str}"
        )

    content = f"""## Session Progress (idle checkpoint)

**Session started:** {time.strftime("%Y-%m-%d %H:%M", time.localtime(state["session_start_time"]))}
**Elapsed:** {elapsed_min:.0f} minutes
**Tool calls:** {state["tool_call_count"]}
**Since last checkpoint:** {state["tools_since_checkpoint"]}

## Recent Activity
{chr(10).join(notable_summary) if notable_summary else "- No notable activity yet"}

## Context Note
This is an automatic idle checkpoint. The agent should supplement this
with a manual summary of decisions, progress, and next steps.
"""

    # Save to session file
    date_str = _today_str()
    ts_str = time.strftime("%H-%M-%S", time.gmtime())
    note_id = f"sessions/idle-{date_str}_{ts_str}"
    note_path = MEMORY_DIR / f"{note_id}.md"

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    note_path.write_text(content)

    # Also save to DB so it's searchable
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from _lazy_imports import save_memory

        save_memory(
            content=content,
            category="sessions",
            title_slug=f"idle-{date_str}_{ts_str}",
            tags=["idle", "checkpoint", "auto"],
            pinned=False,
            safety_wiring=False,
        )
    except Exception:
        logger.warning("Failed to save memory in session_idle")
        pass

    # Reset checkpoint counter
    state["tools_since_checkpoint"] = 0
    state["last_checkpoint_time"] = time.time()
    _save_state(state)

    return {
        "saved": True,
        "note_id": note_id,
        "elapsed_min": round(elapsed_min, 1),
        "tool_call_count": state["tool_call_count"],
    }


def session_end(session_id: str = "") -> dict:
    """Called when session is deleted/ended.

    Saves a final session summary with all notable activity.
    """
    state = _load_state()
    elapsed_min = (time.time() - state["session_start_time"]) / 60

    # Build full activity log
    activity_lines = []
    for t in state["notable_tools"]:
        activity_lines.append(
            f"- `{t['tool']}` at {time.strftime('%H:%M', time.localtime(t['time']))}: {t.get('preview', '')[:80]}"
        )

    content = f"""## Session End Summary

**Session started:** {time.strftime("%Y-%m-%d %H:%M", time.localtime(state["session_start_time"]))}
**Session ended:** {_now_str()}
**Total duration:** {elapsed_min:.0f} minutes
**Total tool calls:** {state["tool_call_count"]}

## All Notable Activity
{chr(10).join(activity_lines) if activity_lines else "- No notable activity"}

## Context Note
This is the final session summary. The agent should add:
1. What was accomplished
2. Key decisions made
3. What's next
4. Any blockers or open questions
"""

    date_str = _today_str()
    ts_str = time.strftime("%H-%M-%S", time.gmtime())
    note_id = f"sessions/end-{date_str}_{ts_str}"
    note_path = MEMORY_DIR / f"{note_id}.md"

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    note_path.write_text(content)

    # Also save to DB so it's searchable
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from _lazy_imports import save_memory

        save_memory(
            content=content,
            category="sessions",
            title_slug=f"end-{date_str}_{ts_str}",
            tags=["session-end", "summary", "auto"],
            pinned=False,
            safety_wiring=False,
        )
    except Exception:
        logger.warning("Failed to save memory in session_end")
        pass

    # Reset state for next session (preserve cumulative total_tool_calls)
    _save_state(
        {
            "tool_call_count": 0,
            "total_tool_calls": state["total_tool_calls"],
            "last_checkpoint_tool_count": 0,
            "last_checkpoint_time": time.time(),
            "session_start_time": time.time(),
            "tools_since_checkpoint": 0,
            "notable_tools": [],
            "last_compaction_time": state.get("last_compaction_time", 0),
        }
    )

    return {
        "saved": True,
        "note_id": note_id,
        "elapsed_min": round(elapsed_min, 1),
        "tool_call_count": state["tool_call_count"],
    }


def _extract_autosave_summary(content: str, tool: str) -> str:
    """Extract a meaningful 1-line summary from an auto-save note's content."""
    import re
    import json as _json

    # Extract the JSON params block from the note
    params_match = re.search(r"## Params\n```json\n(.*?)\n```", content, re.DOTALL)
    if params_match:
        try:
            parsed = _json.loads(params_match.group(1))
            params = parsed if isinstance(parsed, dict) else {}
        except Exception:
            logger.warning("Failed to parse autosave JSON params")
            params = {}
    else:
        params = {}

    # Extract result preview if available
    result_match = re.search(
        r"## Result \(preview\)\n(.+?)(?:\n---|\Z)", content, re.DOTALL
    )
    result = ""
    if result_match:
        result = result_match.group(1).strip()
        if result.startswith("_no result preview_") or not result:
            result = ""

    # Build summary based on tool type
    if tool == "bash":
        cmd = params.get("command", "")
        desc = params.get("description", "")
        if cmd:
            # Get the main part of the command (first meaningful line)
            cmd_clean = cmd.split("\n")[0][:120]
            if desc:
                return f"{desc}: `{cmd_clean}`"
            return f"$ {cmd_clean}"
        if result:
            return result[:120]
    elif tool == "read":
        p = params.get("path", params.get("filePath", ""))
        if p:
            fname = p.split("/")[-1]
            return f"Reading `{fname}`"
    elif tool == "write":
        p = params.get("path", params.get("filePath", ""))
        if p:
            fname = p.split("/")[-1]
            content_preview = params.get("content", "")[:60]
            return f"Writing `{fname}`: {content_preview}"
    elif tool == "edit":
        p = params.get("path", params.get("filePath", ""))
        if p:
            fname = p.split("/")[-1]
            old = params.get("oldString", "")[:40]
            new = params.get("newString", "")[:40]
            return f"Edit `{fname}`: '{old}' -> '{new}'"
    elif tool == "glob":
        pattern = params.get("pattern", "")
        if pattern:
            return f"Searching for `{pattern}`"
    elif tool == "grep":
        pattern = params.get("pattern", "")
        if pattern:
            return f"Grep `{pattern}`"
    elif tool in ("memory_save", "agentic-memory_memory_save"):
        # The MCP server exposes memory_save under the namespaced
        # `agentic-memory_memory_save` when called from opencode. Match
        # both so the survival note captures content either way.
        title = params.get("title_slug", "")
        cat = params.get("category", "")
        content = params.get("content", "")
        if title:
            # Capture the actual content (not just the slug) — these are
            # the agent's conclusions/decisions/lessons, the most valuable
            # data to survive a compaction event. 250 chars is enough to
            # surface the "what was decided" without bloating the survival
            # note with full documents.
            content_preview = content[:250].replace("\n", " ").strip()
            if content_preview:
                return f"Saving {cat}/{title}: {content_preview}"
            return f"Saving {cat}/{title}"
    elif tool == "memory_search":
        query = params.get("query", "")
        if query:
            return f"Searching: {query[:80]}"
    elif tool == "memory_delete":
        note_id = params.get("note_id", "")
        if note_id:
            return f"Deleting: {note_id}"
    elif tool == "memory_recall_context":
        query = params.get("query", "")
        return f"Recalling context: {query[:60]}" if query else "Recalling context"
    elif tool == "memory_check_integrity":
        return "Checking DB integrity"
    elif tool == "memory_graph_stats":
        return "Checking KG stats"
    elif tool == "memory_facts_stats":
        return "Checking facts stats"
    elif tool == "memory_retention_stats":
        return "Checking retention stats"
    elif tool == "memory_tier_stats":
        return "Checking tier stats"
    elif tool == "question":
        q = params.get("question", "")
        if q and isinstance(q, str):
            return q[:100]
    elif tool == "todowrite":
        todos = params.get("todos", [])
        if todos:
            items = [t.get("content", "")[:40] for t in todos[:3] if t.get("content")]
            return f"Todos: {', '.join(items)}"
    elif tool == "task":
        desc = params.get("description", "")
        if desc:
            return f"Task: {desc[:80]}"

    # Fallback to result if available
    if result:
        return result[:120]

    return ""


def _get_session_autosaves(session_start_time: float = 0.0) -> list[dict]:
    """Read auto-save notes from the current session (since session_start_time).

    Returns a list of dicts with tool, time, and content summary.
    """
    if session_start_time <= 0.0:
        today_local = time.strftime("%Y-%m-%d", time.localtime())
        today_utc = time.strftime("%Y-%m-%d", time.gmtime())
        date_prefixes = {today_local, today_utc}

        files: list[Path] = []
        for prefix in date_prefixes:
            files.extend(SESSIONS_DIR.glob(f"auto-{prefix}_*.md"))
        files = sorted(list(set(files)), key=lambda x: x.name)
    else:
        # Filter auto-saves based on mtime (5 minutes buffer before session start)
        start_threshold = session_start_time - 300
        start_local = time.strftime("%Y-%m-%d", time.localtime(start_threshold))
        start_utc = time.strftime("%Y-%m-%d", time.gmtime(start_threshold))
        now_local = time.strftime("%Y-%m-%d", time.localtime())
        now_utc = time.strftime("%Y-%m-%d", time.gmtime())

        date_prefixes = {start_local, start_utc, now_local, now_utc}

        files = []
        for prefix in date_prefixes:
            for f in SESSIONS_DIR.glob(f"auto-{prefix}_*.md"):
                try:
                    if f.stat().st_mtime >= start_threshold:
                        files.append(f)
                except Exception:
                    logger.warning("Failed to stat autosave file %s", f)
                    pass
        files = sorted(list(set(files)), key=lambda x: x.name)

    # Slice to keep only the last 100 files BEFORE we read and parse them from disk
    files = files[-100:]

    autosaves = []
    for f in files:
        try:
            content = f.read_text()
            # Extract tool name from filename: auto-YYYY-MM-DD_HH-MM-SS+TZ-tool.md
            # Use regex to handle tools with hyphens (e.g., agentic-memory_memory_save)
            match = re.match(
                r"auto-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}[+-]\d{2}-\d{2}-(.+)",
                f.stem,
            )
            tool = match.group(1) if match else "unknown"
            summary = _extract_autosave_summary(content, tool)
            autosaves.append(
                {
                    "file": f.name,
                    "tool": tool,
                    "content_preview": summary,
                }
            )
        except Exception:
            logger.warning("Failed to read autosave file %s", f)
            pass
    return autosaves


def _build_activity_log(state: dict) -> str:
    """Format the last 100 notable_tools as a markdown bullet list.

    Extracted from pre_compaction() (2026-06-22) so the orchestrator
    stays readable.
    """
    lines = []
    for t in state["notable_tools"][-100:]:
        preview = t.get("preview", "")
        lines.append(
            f"- `{t['tool']}` at {time.strftime('%H:%M', time.localtime(t['time']))}: {preview}"
        )
    return "\n".join(lines) if lines else "- No notable activity recorded"


def _build_autosave_section(autosaves: list) -> str:
    """Format auto-save records with content details for post-compaction
    recovery. Shows the file, tool, and a content preview so the agent
    can tell what actually changed.
    """
    lines = []
    seen: set[str] = set()
    for a in reversed(autosaves[-50:]):
        tool = a.get("tool", "?")
        fname = a.get("file", "?")
        preview = a.get("content_preview", "")
        dedup_key = f"{fname}:{tool}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        verb = {
            "edit": "Edited",
            "replace_file_content": "Edited",
            "write": "Wrote",
            "write_file": "Wrote",
            "memory_save": "Saved",
            "agentic-memory_memory_save": "Saved",
        }.get(tool.split("_")[-1] if "_" in tool else tool, tool)
        if preview:
            first_line = preview.split("\n")[0].strip()[:120]
            lines.append(f"- {verb} `{fname}`: {first_line}")
        else:
            lines.append(f"- {verb} `{fname}`")
    if not lines:
        return "- No file changes recorded this session"
    return "\n".join(lines)


def _build_work_items(state: dict) -> str:
    """Derive a "what was being worked on" bullet list from the last 100
    tool calls. Maps each tool kind to a one-line markdown summary
    (editing file X, running command Y, etc.) and dedupes by filename
    for read/edit/write operations.

    Extracted from pre_compaction() (2026-06-22) — this contained 19
    `elif` branches and was the bulk of the function.
    """
    work_items: list[str] = []
    seen_files: set[str] = set()

    def _fname_from_preview(preview: str) -> str:
        """Pull a filename out of a tool-call preview string."""
        if "/" in preview:
            return preview.split("/")[-1].split(":")[0]
        return preview[:50]

    for t in state["notable_tools"][-100:]:
        preview = t.get("preview", "")
        if not preview:
            continue
        tool = t.get("tool", "")
        if "edit" in tool or "replace" in tool or "write" in tool:
            fname = _fname_from_preview(preview)
            if fname not in seen_files:
                seen_files.add(fname)
                verb = (
                    "Editing" if "edit" in tool or "replace" in tool else "Writing to"
                )
                work_items.append(f"- {verb} `{fname}`")
        elif "read" in tool or "view" in tool:
            if "`" in preview:
                fname = preview.split("`")[-2]
            elif "'" in preview:
                fname = preview.split("'")[1]
            else:
                fname = preview[:50]
            if fname not in seen_files:
                seen_files.add(fname)
                work_items.append(f"- Reading `{fname}`")
        elif "bash" in tool or "run_command" in tool:
            cmd = (
                preview.lstrip("$ ")
                .strip()
                .replace("run `", "")
                .rstrip("`")
                .strip()[:100]
            )
            if cmd and len(cmd) > 3:
                work_items.append(f"- Running command: `{cmd}`")
        elif "test" in tool:
            work_items.append(f"- Running tests: {preview}")
        elif "coverage" in tool:
            work_items.append(f"- Checking test coverage: {preview}")
        elif "security" in tool:
            work_items.append(f"- Running security audit: {preview}")
        elif "format" in tool:
            work_items.append(f"- Formatting code: {preview}")
        elif "lint" in tool:
            work_items.append(f"- Lint checking: {preview}")
        elif "git" in tool:
            work_items.append(f"- Git command: {preview}")
        elif "changed" in tool:
            work_items.append(f"- Checking changed files: {preview}")
        elif "grep" in tool or "glob" in tool:
            work_items.append(f"- Searching codebase: {preview}")
        elif "search_web" in tool or "web_search" in tool:
            work_items.append(f"- Web search: {preview}")
        elif "memory_save" in tool:
            work_items.append(f"- Saving memory: {preview[:60]}")
        elif "memory_delete" in tool:
            work_items.append(f"- Deleting memory: {preview[:60]}")
        elif "task" in tool:
            work_items.append(f"- Task: {preview[:60]}")
        elif "todowrite" in tool:
            work_items.append(f"- Updated todos: {preview[:60]}")
        elif "memory_graph" in tool or "memory_temporal" in tool:
            work_items.append(f"- KG query: {preview[:80]}")
        elif "memory_create_entities" in tool or "memory_add_observations" in tool:
            work_items.append(f"- KG write: {preview[:80]}")
        elif "memory_search_nodes" in tool or "memory_open_nodes" in tool:
            work_items.append(f"- KG search: {preview[:80]}")

    return (
        "\n".join(work_items) if work_items else "- No file edits or commands detected"
    )


def _synthesize_session_summary(
    autosaves: list[dict], notable_tools: list[dict], state: dict
) -> dict[str, str]:
    """Derive structured session sections from tool activity, so compaction
    produces useful content even when the agent never called memory_save.

    Produces four sections:
      conclusions: deduplicated high-signal autosave entries (edit/write/task/
        question/memory_save summaries) trimmed to unique actions.
      insights:    short sentences parsed from git commits, test results, and
        explicit memory_save content containing decision/lesson keywords.
      todos:       the most recent todowrite preview, fallback to statements
        containing "need to" or "should" from recent bash/edit activity.
      next_steps:  sentences containing "next", "blocker", or "TODO" from
        recent memory_save and bash command previews.

    This is the structural fix for empty compaction sections: instead of
    depending only on explicit memory_save calls, we derive meaning from
    the tool activity that was always being captured.
    """
    recent_notable = notable_tools[-80:] if notable_tools else []
    recent_autos = autosaves[-60:] if autosaves else []

    # --- Conclusions: meaningful file edits, writes, tasks, questions, saves ---
    seen_conclusions: set[str] = set()
    conclusions: list[str] = []
    for a in reversed(recent_autos):
        tool = a.get("tool", "")
        preview = a.get("content_preview", "").strip()
        if not preview:
            continue
        if tool not in {
            "edit",
            "write",
            "write_file",
            "write_to_file",
            "replace_file_content",
            "multi_replace_file_content",
            "memory_save",
            "agentic-memory_memory_save",
            "task",
            "question",
            "agentic-memory_question",
            "todowrite",
            "agentic-memory_todowrite",
        }:
            continue
        # Normalise and dedupe
        normalised = preview[:180]
        if normalised not in seen_conclusions:
            seen_conclusions.add(normalised)
            conclusions.append(normalised)
        if len(conclusions) >= 6:
            break
    conclusions_section = (
        "\n".join(f"- {c}" for c in conclusions)
        if conclusions
        else "- No conclusions derived from tool activity this session"
    )

    # --- Insights: decision/lesson keywords in memory_save content and git commits ---
    insights: list[str] = []
    for a in reversed(recent_autos):
        tool = a.get("tool", "")
        preview = a.get("content_preview", "").lower()
        if tool in ("memory_save", "agentic-memory_memory_save") and any(
            k in preview
            for k in (
                "decision",
                "lesson",
                "conclusion",
                "resolved",
                "fixed",
                "changed",
            )
        ):
            raw = a.get("content_preview", "").strip()
            if raw and raw not in insights:
                insights.append(raw[:200])
        if len(insights) >= 4:
            break
    # Also pull from git commits in bash activity
    for t in reversed(recent_notable):
        tool = t.get("tool", "")
        preview = t.get("preview", "")
        if tool in ("bash", "agentic-memory_bash") and "git commit" in preview.lower():
            msg = preview.lower().split("git commit", 1)[-1].strip().strip("-").strip()
            if msg and msg not in insights:
                insights.append(f"Git commit: {msg[:120]}")
        if len(insights) >= 4:
            break
    insights_section = (
        "\n".join(f"- {i}" for i in insights[:3])
        if insights
        else "- No insights derived from tool activity this session"
    )

    # --- Todos: most recent todowrite preview ---
    todos: list[str] = []
    for t in reversed(recent_notable):
        tool = t.get("tool", "")
        if tool in ("todowrite", "agentic-memory_todowrite"):
            preview = t.get("preview", "").strip()
            if preview and len(preview) > 5:
                todos.append(preview[:200])
                break
    todos_section = (
        f"- {todos[0]}"
        if todos
        else "- No todo list derived from tool activity this session"
    )

    # --- Next steps: sentences with forward-looking keywords ---
    next_steps: list[str] = []
    for a in reversed(recent_autos):
        tool = a.get("tool", "")
        preview = a.get("content_preview", "")
        if tool not in (
            "memory_save",
            "agentic-memory_memory_save",
            "bash",
            "agentic-memory_bash",
        ):
            continue
        text = (
            preview
            if tool in ("memory_save", "agentic-memory_memory_save")
            else preview
        )
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            s = sentence.strip()
            if len(s) < 10:
                continue
            if any(
                k in s.lower()
                for k in ("next", "blocker", "todo", "need to", "should", "remaining")
            ):
                if s not in next_steps:
                    next_steps.append(s[:200])
        if len(next_steps) >= 3:
            break
    # Fallback: scan notable_tools previews if autosaves yielded nothing
    if not next_steps:
        for t in reversed(recent_notable):
            tool = t.get("tool", "")
            preview = t.get("preview", "")
            if tool not in (
                "bash",
                "agentic-memory_bash",
                "memory_save",
                "agentic-memory_memory_save",
                "task",
                "question",
                "agentic-memory_question",
                "todowrite",
                "agentic-memory_todowrite",
            ):
                continue
            for sentence in re.split(r"(?<=[.!?])\s+", preview):
                s = sentence.strip()
                if len(s) < 10:
                    continue
                if any(
                    k in s.lower()
                    for k in (
                        "next",
                        "blocker",
                        "todo",
                        "need to",
                        "should",
                        "remaining",
                    )
                ):
                    if s not in next_steps:
                        next_steps.append(s[:200])
            if len(next_steps) >= 3:
                break
    next_steps_section = (
        "\n".join(f"- {s}" for s in next_steps[:3])
        if next_steps
        else "- No next steps derived from tool activity this session"
    )

    return {
        "conclusions": conclusions_section,
        "insights": insights_section,
        "todos": todos_section,
        "next_steps": next_steps_section,
    }


def _extract_recent_conclusions(autosaves: list, max_count: int = 5) -> list[str]:
    """Return up to ``max_count`` most-recent memory_save content previews
    in reverse-chronological order, deduped. Used by the
    "Recent Conclusions" section of the compaction note.

    Extracted from pre_compaction() (2026-06-22).
    """
    conclusions: list[str] = []
    for a in reversed(autosaves):
        if a.get("tool") in ("memory_save", "agentic-memory_memory_save"):
            preview = a.get("content_preview", "")
            if preview and preview not in conclusions:
                conclusions.append(preview)
        if len(conclusions) >= max_count:
            break
    return conclusions


def _build_rich_context_sections(
    state: dict, autosaves: list, recent_conclusions: list[str]
) -> dict[str, str]:
    """Build the four "rich context" sections for the compaction note:

    1. Recent Conclusions (already filtered, just format)
    2. Key Insights (filter conclusions for decision/lesson/conclusion)
    3. Active Todos (most-recent todowrite call's content)
    4. Next Steps (sentences containing "next" or "blocker" in conclusions)

    Extracted from pre_compaction() (2026-06-22) — these are the
    sections a fresh agent post-compaction actually needs.
    """
    conclusions_section = (
        "\n".join(f"- {c}" for c in recent_conclusions)
        if recent_conclusions
        else "- No memory_save calls captured this session"
    )

    key_insights = [
        c
        for c in recent_conclusions
        if any(k in c.lower() for k in ("decision", "lesson", "conclusion"))
    ]
    insights_section = (
        "\n".join(f"- {i}" for i in key_insights[:3])
        if key_insights
        else "- No decisions or lessons saved this session"
    )

    active_todos: list[str] = []
    for t in reversed(state["notable_tools"]):
        if t.get("tool") in ("todowrite", "agentic-memory_todowrite"):
            preview = t.get("preview", "")
            if preview and len(preview) > 5:
                active_todos.append(preview)
                break
    todos_section = (
        f"- {active_todos[0]}"
        if active_todos
        else "- No todo list captured this session"
    )

    next_steps: list[str] = []
    for c in recent_conclusions:
        if "next" in c.lower() or "todo" in c.lower() or "blocker" in c.lower():
            for s in re.split(r"(?<=[.!?])\s+", c):
                if "next" in s.lower() or "blocker" in s.lower():
                    next_steps.append(s.strip())
                    break
    next_steps_section = (
        "\n".join(f"- {s[:200]}" for s in next_steps[:3])
        if next_steps
        else "- No explicit next steps captured"
    )

    return {
        "conclusions": conclusions_section,
        "insights": insights_section,
        "todos": todos_section,
        "next_steps": next_steps_section,
    }


COMPACTION_PIN_LIMIT = 10
COMPACTION_RETENTION_DAYS = 3


def _compaction_note_age_days(note_id: str) -> float | None:
    """Extract timestamp from a compaction note ID like
    'sessions/compaction-save-2026-06-25_18-55-02'.
    Returns age in days, or None if parse fails.
    """
    try:
        slug = note_id.rsplit("/", 1)[-1]  # compaction-save-2026-06-25_18-55-02
        date_time = slug.replace("compaction-save-", "")  # 2026-06-25_18-55-02
        ts = time.strptime(date_time, "%Y-%m-%d_%H-%M-%S")
        epoch = time.mktime(ts)
        return (time.time() - epoch) / 86400
    except Exception:
        return None


def _enforce_compaction_pin_limit():
    """Cap pinned compaction notes at COMPACTION_PIN_LIMIT and delete
    compaction notes older than COMPACTION_RETENTION_DAYS from both
    the DB and the filesystem.
    """
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from memory_common import get_memory_paths, open_db

        _, local_mem, _ = get_memory_paths()
        db_path = local_mem / "memory.db"
        sessions_dir = MEMORY_DIR

        with open_db(db_path, write=True) as conn:
            rows = conn.execute(
                "SELECT id FROM memories "
                "WHERE category='sessions' AND pinned=1 AND id LIKE 'sessions/compaction-save-%' "
                "ORDER BY created_at DESC"
            ).fetchall()
            to_unpin = [r[0] for r in rows[COMPACTION_PIN_LIMIT:]]
            if to_unpin:
                conn.executemany(
                    "UPDATE memories SET pinned=0 WHERE id=?", [(i,) for i in to_unpin]
                )
                logger.info(
                    "compaction pin limit: unpinned %d old compaction saves, kept %d",
                    len(to_unpin),
                    min(COMPACTION_PIN_LIMIT, len(rows)),
                )

            all_rows = conn.execute(
                "SELECT id FROM memories "
                "WHERE category='sessions' AND id LIKE 'sessions/compaction-save-%'"
            ).fetchall()
            to_delete = []
            for (note_id,) in all_rows:
                age = _compaction_note_age_days(note_id)
                if age is not None and age > COMPACTION_RETENTION_DAYS:
                    to_delete.append(note_id)

            if to_delete:
                for note_id in to_delete:
                    md = sessions_dir / f"{note_id}.md"
                    if md.exists():
                        md.unlink()
                    conn.execute("DELETE FROM memories WHERE id=?", (note_id,))
                logger.info(
                    "compaction retention: deleted %d notes older than %d days",
                    len(to_delete),
                    COMPACTION_RETENTION_DAYS,
                )

            valid_ids = set()
            for (nid,) in conn.execute(
                "SELECT id FROM memories WHERE id LIKE 'sessions/compaction-save-%'"
            ).fetchall():
                valid_ids.add(nid)

        orphan_count = 0
        for md in sessions_dir.glob("compaction-save-*.md"):
            note_id = "sessions/" + md.stem
            if note_id not in valid_ids:
                try:
                    md.unlink()
                    orphan_count += 1
                except Exception:
                    pass
        if orphan_count:
            logger.info(
                "compaction retention: removed %d orphaned .md files", orphan_count
            )

    except Exception as e:
        logger.warning("compaction pin limit / retention: %s", e)


def _write_compaction_note(
    content: str,
    autosaves: list,
    state: dict,
    elapsed_min: float,
    message_count: int,
    rich_summary: dict | None = None,
) -> dict:
    """Write the compaction note to both the markdown filesystem AND
    the live memory DB via save_pipeline. The filesystem path is
    always written; the DB save is best-effort (the file is the
    authoritative copy).

    Returns the result dict the caller returns to the caller.
    Extracted from pre_compaction() (2026-06-22).
    """
    date_str = _today_str()
    ts_str = time.strftime("%H-%M-%S", time.gmtime())
    note_id = f"sessions/compaction-save-{date_str}_{ts_str}"
    note_path = MEMORY_DIR / f"{note_id}.md"

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    note_path.write_text(content)

    # Also save to the main memory DB via save_pipeline. Pin so the
    # compaction note survives future purges — they're critical.
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from _lazy_imports import save_memory

        save_memory(
            content=content,
            category="sessions",
            title_slug=f"compaction-save-{date_str}_{ts_str}",
            tags=["compaction", "context-save", "auto"],
            pinned=True,
            safety_wiring=False,
        )
        _enforce_compaction_pin_limit()
    except Exception:
        logger.warning(
            "Failed to save compaction memory to DB, file-based save already succeeded"
        )

    if rich_summary:
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from _lazy_imports import save_memory as _save_mem2

            derived_tags = ["compaction", "session-summary", "derived", "auto"]
            if rich_summary.get("todos", "").startswith("- No todo"):
                derived_tags.append("no-todos")
            else:
                derived_tags.append("has-todos")
            _save_mem2(
                content=(
                    f"## Session Summary (auto-derived at compaction)\n\n"
                    f"**Session duration:** {elapsed_min:.0f} minutes\n"
                    f"**Tool calls:** {state.get('tool_call_count', 0)}\n\n"
                    f"### Conclusions\n{rich_summary.get('conclusions', '')}\n\n"
                    f"### Insights\n{rich_summary.get('insights', '')}\n\n"
                    f"### Todos\n{rich_summary.get('todos', '')}\n\n"
                    f"### Next Steps\n{rich_summary.get('next_steps', '')}\n"
                ),
                category="sessions",
                title_slug=f"session-summary-{_today_str()}-{time.strftime('%H-%M', time.gmtime())}",
                tags=derived_tags,
                pinned=True,
                importance=0.9,
                safety_wiring=False,
            )
        except Exception:
            logger.warning("Failed to save derived session summary to DB")

    return {
        "saved": True,
        "note_id": note_id,
        "elapsed_min": round(elapsed_min, 1),
        "tool_call_count": state["tool_call_count"],
        "total_tool_calls": state.get("total_tool_calls", state["tool_call_count"]),
        "autosaves_included": len(autosaves),
        "message_count": message_count,
    }


def pre_compaction(session_id: str = "", message_count: int = 0) -> dict:
    """Called BEFORE compaction starts (experimental.session.compacting hook).

    This is the most critical function. It saves context that would
    otherwise be lost during compaction. Reads accumulated auto-save
    notes to build a richer context snapshot.
    Decomposed 2026-06-22 — orchestrator below delegates to:

      * _build_activity_log
      * _build_autosave_section
      * _build_work_items
      * _synthesize_session_summary   ← now derives conclusions/insights/todos/next_steps
                                         from tool activity so sections are never empty
      * _write_compaction_note
    """
    # Dedup: OpenCode fires compacting hook multiple times per compaction.
    # Skip if we already saved a compaction note in the last 45 seconds.
    dedup_state = _load_state()
    last_compaction = dedup_state.get("last_compaction_time", 0)
    if time.time() - last_compaction < 45:
        return {"deduped": True, "last_compaction": last_compaction}

    # Single save: set compaction timestamp and reset checkpoint counter
    # together so no interleaving writer can clobber either field.
    state = _load_state()
    state["last_compaction_time"] = time.time()
    state["tools_since_checkpoint"] = 0
    state["last_checkpoint_time"] = time.time()
    _save_state(state)

    elapsed_min = (time.time() - state["session_start_time"]) / 60

    activity_section = _build_activity_log(state)

    autosaves = _get_session_autosaves(state.get("session_start_time", 0.0))
    autosave_section = _build_autosave_section(autosaves)
    work_section = _build_work_items(state)

    # Reset checkpoint counter so next session starts fresh
    state["tools_since_checkpoint"] = 0
    state["last_checkpoint_time"] = time.time()
    # last_compaction_time was already written at line 1269 and saved at line 1272.
    # Do NOT overwrite it with dedup_state (holds the stale pre-save value).
    _save_state(state)

    recent_conclusions = _synthesize_session_summary(
        autosaves, state.get("notable_tools", []), state
    )
    rich = recent_conclusions

    content = f"""## Pre-Compaction Context Save

**This session is about to be compacted. Early context will be lost.**
**Read this note FIRST after compaction, before any other action.**

**Session started:** {time.strftime("%Y-%m-%d %H:%M", time.localtime(state["session_start_time"]))}
**Time before compaction:** {elapsed_min:.0f} minutes
**Total tool calls (this session):** {state["tool_call_count"]}
**Total tool calls (cumulative):** {state.get("total_tool_calls", state["tool_call_count"])}
**Estimated messages:** {message_count}

## Recent Conclusions (read these first)
These are the agent's actual conclusions/decisions/lessons saved during
this session via `memory_save`. They are the highest-value content for
post-compaction continuity — the agent *already decided* what mattered.
{rich["conclusions"]}

## Key Insights (decisions/lessons)
{rich["insights"]}

## Active Todos
{rich["todos"]}

## Next Steps (from agent's own forward-pointing notes)
{rich["next_steps"]}

## What Was Being Worked On
{work_section}

## Recent Tool Activity
{activity_section}

## Auto-Save Notes (with content summaries)
{autosave_section}

## Recovery: What to Do After Compaction
1. **Read "Recent Conclusions" and "Key Insights" FIRST** — these are the
   agent's synthesized knowledge from before compaction
2. **Check "Active Todos"** to see what was in progress
3. The files listed in "What Was Being Worked On" likely need attention
4. Search for related context: `memory_search(query="recent session activity")`
5. Continue from where you left off

**Related:** [[projects/agentic-memory-audit-complete]]
"""

    result = _write_compaction_note(
        content, autosaves, state, elapsed_min, message_count, rich_summary=rich
    )

    # Sprint 3: log compaction to the session entity via SessionManager
    try:
        from session_manager import SessionManager

        mgr = SessionManager()
        cs_path = SESSIONS_DIR / ".current_session.json"
        cs = {}
        if cs_path.exists():
            try:
                cs = json.loads(cs_path.read_text())
            except Exception:
                pass
        entity_session_id = cs.get("session_id", "")
        if entity_session_id:
            summary_note_id = result.get("note_id", "")
            tokens_before = message_count * 200
            recovered_ids = []
            for a in autosaves:
                try:
                    ac = a.get("content_preview", "")
                    m = re.search(r"note_id[:\s]+([\w-]+)", ac)
                    if m:
                        recovered_ids.append(m.group(1))
                except Exception:
                    pass
            mgr.compact_session(
                session_id=entity_session_id,
                tokens_before=tokens_before,
                tokens_after=len(content) // 4,
                summary_note_id=summary_note_id,
                recovered_note_ids=recovered_ids,
            )
    except ImportError:
        pass
    except Exception as _e:
        logger.warning("pre_compaction: SessionManager.compact_session failed: %s", _e)

    return result


def get_status() -> dict:
    """Get current monitoring status."""
    state = _load_state()
    elapsed_min = (time.time() - state["session_start_time"]) / 60

    return {
        "tool_call_count": state["tool_call_count"],
        "total_tool_calls": state.get("total_tool_calls", state["tool_call_count"]),
        "tools_since_checkpoint": state["tools_since_checkpoint"],
        "checkpoint_interval": CHECKPOINT_INTERVAL,
        "next_checkpoint_in": max(
            0, CHECKPOINT_INTERVAL - state["tools_since_checkpoint"]
        ),
        "elapsed_min": round(elapsed_min, 1),
        "session_start": time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(state["session_start_time"])
        ),
        "notable_tools_count": len(state["notable_tools"]),
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Context Monitor for Agentic Memory")
    sub = parser.add_subparsers(dest="command")

    track = sub.add_parser("track", help="Track a tool call")
    track.add_argument("--tool", required=True)
    track.add_argument("--params", default="{}")
    track.add_argument("--result-preview", default="")

    idle = sub.add_parser("idle", help="Session idle checkpoint")
    idle.add_argument("--session-id", default="")

    end = sub.add_parser("end", help="Session end summary")
    end.add_argument("--session-id", default="")

    compact = sub.add_parser("compact", help="Pre-compaction save")
    compact.add_argument("--session-id", default="")
    compact.add_argument("--message-count", type=int, default=0)

    sub.add_parser("status", help="Show monitoring status")

    args = parser.parse_args()

    if args.command == "track":
        result = track_tool_call(args.tool, args.params, args.result_preview)
        print(json.dumps(result, indent=2))
    elif args.command == "idle":
        result = session_idle(args.session_id)
        print(json.dumps(result, indent=2))
    elif args.command == "end":
        result = session_end(args.session_id)
        print(json.dumps(result, indent=2))
    elif args.command == "compact":
        result = pre_compaction(args.session_id, args.message_count)
        print(json.dumps(result, indent=2))
    elif args.command == "status":
        result = get_status()
        print(json.dumps(result, indent=2))
    else:
        parser.print_help()
