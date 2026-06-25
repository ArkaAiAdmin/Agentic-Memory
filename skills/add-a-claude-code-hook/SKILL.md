---
name: add-a-claude-code-hook
description: Maintainer procedure for adding a new Claude Code lifecycle hook to the agentic-memory system. Use when an event (session start, pre-tool, post-tool, session end) should automatically do something the agent shouldn't have to remember to do. Don't use for scheduled background jobs (that's `add-a-cron-job`).
---

# Add a Claude Code Hook

How to add a new lifecycle hook to the agentic-memory system. There are 4 hooks today (3 user-facing + 1 log redirect); this is how to add a 5th.

## The 60-second version

1. Create `hooks/memory_your_event.py` at the repo root.
2. Wire it into the user's Claude Code config (typically `~/.claude/settings.json` or opencode config).
3. The hook receives JSON on stdin, prints to **stdout** (NOT stderr — that was a 2-day bug).
4. Test with a manual JSON invocation: `echo '{"tool_name":"Bash","tool_input":{"command":"ls"}}' | python hooks/memory_your_event.py`

Total: ~30 minutes for a read-only hook, ~2 hours for one that mutates state.

## Lifecycle event types

| Event | When it fires | What you get | What you can do |
|---|---|---|---|
| `SessionStart` | New session begins | session_id, cwd, hook_event_name | Inject context, load memory |
| `UserPromptSubmit` | User types a message | prompt, session_id, cwd | Pre-process prompt, inject context |
| `PreToolUse` | Before any tool call | tool_name, tool_input, session_id | Modify tool_input, inject context, deny |
| `PostToolUse` | After any tool call | tool_name, tool_input, tool_output, session_id | React to result, save, validate |
| `Stop` | Agent stops responding | session_id, last_user_message, last_assistant_message | Save snapshot, ping user |
| `SessionEnd` | Session deleted | session_id, reason, duration_seconds | Final save, cleanup |

The current 4 hooks: `memory-proactive-context.py` (PreToolUse), `memory-search-on-demand.py`, `memory-session-start.py` (SessionStart), and `_log_error.py` (log redirect). The auto-save flow runs on PostToolUse via `auto_save.py`.

## Step 1: write the hook

```python
#!/usr/bin/env python3
"""Hook: <event> — what this does in one line.

Triggered by: <where in claude config>
Input: JSON on stdin with <list of fields>
Output: <what to print, where>
"""
import sys
import json
import os

# Use the user's Python (M8 fix)
sys.path.insert(0, os.path.expanduser("~/.config/agentic-memory"))

def main():
    try:
        # Read input
        raw = sys.stdin.read()
        data = json.loads(raw) if raw else {}
    except Exception as e:
        # NEVER crash a hook silently. Print to stderr, return.
        print(f"hook: failed to parse stdin: {e}", file=sys.stderr)
        return

    # Extract what you need
    event = data.get("hook_event_name", "unknown")
    session_id = data.get("session_id", "unknown")
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    # Do your work
    result = your_logic(event, session_id, tool_name, tool_input)

    # Print to STDOUT — this is what Claude Code injects
    print(result)

if __name__ == "__main__":
    main()
```

**The 4 most important rules:**

1. **Print to STDOUT, never stderr.** Claude Code reads stdout as the hook's output and injects it. Stderr is for debug logs only. The proactive-context hook was broken for 2 days because of this.

2. **Don't crash.** Wrap everything in `try/except`. If your hook fails, the agent should still work. Print the error to stderr and return gracefully.

3. **Be fast.** Hooks run synchronously. A 5-second hook blocks the agent. Cache aggressively, do I/O sparingly.

4. **Idempotent.** A hook may fire multiple times for the same event. Use dedup windows (the pre-compaction hook uses 45s).

## Step 2: implement your logic

Common patterns:

**Read-only (most common — inject context):**

```python
def your_logic(event, session_id, tool_name, tool_input):
    from memory_mcp import search_memories
    from memory_common import get_memory_paths
    from pathlib import Path

    # Extract a query from the event
    query = tool_input.get("prompt") or tool_input.get("command") or ""
    if not query:
        return ""

    # Search
    _, local_mem, _ = get_memory_paths()
    db_path = local_mem / "memory.db"
    if not db_path.exists():
        return ""

    try:
        results = search_memories(query, limit=3, db_path=str(db_path))
    except Exception as e:
        print(f"hook: search failed: {e}", file=sys.stderr)
        return ""

    # Format for the agent
    if not results:
        return ""
    return "[Memory context]\n" + "\n".join(f"- {r['content'][:200]}" for r in results)
```

**Write (e.g., save state on every tool call):**

```python
def your_logic(event, session_id, tool_name, tool_input):
    from auto_save import tool_complete
    from memory_common import get_memory_paths
    from pathlib import Path

    _, local_mem, _ = get_memory_paths()
    db_path = local_mem / "memory.db"
    if not db_path.exists():
        return ""

    try:
        tool_complete(
            tool_name=tool_name,
            params=json.dumps(tool_input),
            preview=extract_preview(tool_name, tool_input),
            db_path=str(db_path),
        )
    except Exception as e:
        print(f"hook: save failed: {e}", file=sys.stderr)
    return ""
```

## Step 3: wire it into the user's Claude Code config

For Claude Code, the config is at `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.config/agentic-memory/hooks/memory_your_event.py",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

For opencode, the hooks are wired in `ecc-hooks.ts` (the opencode plugin). The `ecc-hooks.ts` is in `/Users/arka/.opencode/`. Add your hook to the appropriate event handler.

For the opencode harness, the pattern is:

```typescript
// in ecc-hooks.ts
{
  event: "PreToolUse",
  command: `python3 ${AGENTIC_MEMORY_HOME}/hooks/memory_your_event.py`,
  fireAndForget: false,  // true = async (PostToolUse), false = sync (PreToolUse)
}
```

`fireAndForget: true` for PostToolUse-like hooks (don't block the agent). `fireAndForget: false` for PreToolUse-like hooks (output is injected).

## Step 4: test it manually

```bash
# 1. Smoke test with synthetic input
echo '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"ls -la"},"session_id":"test-123"}' | python3 hooks/memory_your_event.py

# 2. Verify it produces the right output
# Should print context to stdout, errors to stderr, nothing to either if no-op.

# 3. If it modifies state, verify state with an MCP tool
# Example: if it saves a memory, search for it:
echo '{"query":"your test query"}' | python3 -c "
import json, sys
sys.path.insert(0, '/Users/arka/.config/agentic-memory')
from memory_mcp import search_memories
print(search_memories('your test query'))
"
```

## Step 5: add a test (if the hook is non-trivial)

```python
# eval/test_hook_your_event.py
import subprocess, json

class TestYourHook:
    def test_pre_tool_use(self):
        input_data = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "session_id": "test-pretool",
        }
        result = subprocess.run(
            ["python3", "hooks/memory_your_event.py"],
            input=json.dumps(input_data),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        # Assert on stdout (the injected context)
        # Assert on stderr (any error messages)
        # Assert on side effects (DB, files)

    def test_handles_empty_input(self):
        result = subprocess.run(
            ["python3", "hooks/memory_your_event.py"],
            input="",
            capture_output=True,
            text=True,
            timeout=5,
        )
        # Hook should not crash on empty input
        assert result.returncode == 0
```

## Step 6: update memory_workflow.md

In the "Hook System" section, add a row:

```markdown
| `memory_your_event` | <when> | `<subprocess invocation>` | <state change> |
```

## Common pitfalls

- **Print to STDOUT, not stderr.** This is the #1 hook bug. Stdout is what Claude Code reads as the hook's contribution to context.
- **Don't be slow.** A 10-second hook blocks the agent. Cache, batch, dedup.
- **Don't crash.** Wrap in try/except. Print error to stderr. Return.
- **Don't bypass the connection pool.** Use `memory_common.connection_pool.get(str(db_path))` and `safe_close_db(conn)`.
- **Don't write to memory.db directly from a hook.** Use the MCP tools or `save_pipeline.save_memory`. The hook is a UI layer, not a data layer.
- **Don't fire-and-forget for PreToolUse.** If you fire-and-forget a PreToolUse hook, its output is lost. The agent never sees the context.
- **Use `matcher` in the Claude Code config to limit which tools trigger your hook.** `"matcher": "Edit|Write"` is much cheaper than `"matcher": ".*"`.

## Reference

- All 4 existing hooks: `hooks/memory-proactive-context.py`, `hooks/memory-search-on-demand.py`, `hooks/memory-session-start.py`, `hooks/_log_error.py`
- Hook config in Claude Code: `~/.claude/settings.json`
- Hook config in opencode: `/Users/arka/.opencode/ecc-hooks.ts`
- Auto-save hook: `auto_save.py:285` (the `tool_complete` function)
- Hook architecture: `memory_workflow.md` (Hook System section)
- Stdout-vs-stderr bug history: see `agentic-memory-features-on-wiring-fixed` memory note (2026-06-15)

— last reviewed 2026-06-20
