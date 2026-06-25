# How to Add a Claude Code Hook

Add a new lifecycle hook to the agentic-memory system. There are 4 hooks today (`memory-proactive-context`, `memory-search-on-demand`, `memory-session-start`, `memory-recall-session`) + 1 log helper; this is how to add a 5th.

This is the **maintainer** version. For the high-level skill, see `skills/add-a-claude-code-hook/SKILL.md`.

## When to use this

- An event (session start, pre-tool, post-tool, session end) should automatically do something the agent shouldn't have to remember to do.
- The hook's output should be injected as context for the agent.

## When NOT to use this

- You need a recurring task (use `add-a-cron-job`).
- You need a one-shot tool (just write a script).

## Lifecycle events

| Event | When | Best for |
|---|---|---|
| `SessionStart` | New session begins | Inject context, load memory |
| `UserPromptSubmit` | User types a message | Pre-process, inject context |
| `PreToolUse` | Before any tool call | Modify input, inject context, deny |
| `PostToolUse` | After any tool call | React to result, save, validate |
| `Stop` | Agent stops responding | Save snapshot, ping user |
| `SessionEnd` | Session deleted | Final save, cleanup |

The current 4 hooks cover:
- `PreToolUse` — `memory-proactive-context` (proactive context injection)
- `PostToolUse` — `memory-search-on-demand` (auto-save on tool complete)
- `SessionStart` — `memory-session-start` (load context for a fresh session)
- `UserPromptSubmit` — `memory-recall-session` (recall on user prompt)

Plus a 5th file (`_log_error.py`) that is a shared error logger, not a lifecycle hook.

## Steps

1. **Create the hook script** in `hooks/memory_your_event.py`:

   ```python
   #!/usr/bin/env python3
   """Hook: <event> — what this does in one line."""
   import sys
   import json
   import os

   sys.path.insert(0, os.path.expanduser("~/.config/agentic-memory"))

   def main():
       try:
           raw = sys.stdin.read()
           data = json.loads(raw) if raw else {}
       except Exception as e:
           print(f"hook: failed to parse stdin: {e}", file=sys.stderr)
           return

       event = data.get("hook_event_name", "")
       # your logic
       result = your_logic(event, data)
       print(result)  # ← STDOUT, not stderr

   if __name__ == "__main__":
       main()
   ```

2. **The 4 critical rules:**
   - **Print to STDOUT.** Claude Code reads stdout as the hook's output. Stderr is for debug only. (This was a 2-day bug.)
   - **Don't crash.** Wrap in try/except. Return on error.
   - **Be fast.** Hooks are synchronous. 5s = blocked agent.
   - **Be idempotent.** Hooks may fire multiple times.

3. **Wire it into the user's Claude Code config** (`~/.claude/settings.json`):

   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "Bash|Read|Edit",
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

   For opencode, add to `ecc-hooks.ts` (`~/.opencode/ecc-hooks.ts`):

   ```typescript
   {
     event: "PreToolUse",
     command: `python3 ${AGENTIC_MEMORY_HOME}/hooks/memory_your_event.py`,
     fireAndForget: false,  // false = sync (output injected)
   }
   ```

4. **Test manually:**

   ```bash
   echo '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"ls"},"session_id":"test"}' | python3 hooks/memory_your_event.py
   ```

5. **Add a test** in `eval/test_hook_your_event.py`.

6. **Update `memory_workflow.md`** (Hook System section) with a row for your hook.

## Common pitfalls

- **Print to STDOUT, not stderr.** Most common bug.
- **Don't fire-and-forget for PreToolUse.** Output is lost.
- **Don't bypass the connection pool.** Use `connection_pool.get` and `safe_close_db`.
- **Don't write to memory.db directly.** Use MCP tools or `save_pipeline.save_memory`.
- **Use `matcher` to limit which tools trigger your hook.** `"matcher": "Edit|Write"` is much cheaper than `"matcher": ".*"`.

## Reference

- All 3 existing hooks: `hooks/memory-*.py`
- Auto-save hook: `auto_save.py:285` (`tool_complete` function)
- Claude Code config: `~/.claude/settings.json`
- OpenCode config: `~/.opencode/ecc-hooks.ts`
- Skill (deeper version): `skills/add-a-claude-code-hook/SKILL.md`
