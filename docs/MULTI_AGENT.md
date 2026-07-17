# Multi-Agent Memory Setup

You are one of several agents sharing an agentic-memory backend. Every agent
writes to their own DB partition; shared content flows through a cross-agent
pool so one agent's high-signal memories are discoverable by the other.

## Your Identity

`MEMORY_AGENT_ID` is set in your MCP server env. All writes are scoped to this
identity. To confirm yours:

```
memory_profile(action="agents")
```

## Multi-Agent Tools

### `memory_share` — The Cross-Agent Router

This single tool does four things depending on `action=`:

| `action` | What it does | Required params |
|----------|-------------|-----------------|
| `"list"` | List shared memories from all agents (default) | *(none)* |
| `"share"` | Push one of your memories into the shared pool | `note_id` + `share_with` |
| `"import"` | Pull a shared memory into your own DB | `note_id` (shared ID) + `share_with` (your agent ID) |
| `"stats"` | Overview: how many shared, by whom, categories | *(none)* |

**Examples:**

```python
# See what other agents have shared
memory_share(action="list")

# Share a lesson about something MIMOCODE should know
memory_share(note_id="lessons/api-deadlock-fix", share_with="MIMOCODE", action="share")

# Pull a shared decision into your own DB
memory_share(note_id="decisions/cache-strategy", share_with="OPENCODE", action="import")

# Check sharing stats
memory_share(action="stats")
```

**Tip:** `share_with` is the target agent's `MEMORY_AGENT_ID`. The pool is
scoped per target — a memory shared with `MIMOCODE` won't appear for
`OPENCODE` unless shared with both.

### `memory_search(shared_with_me=True)`

Add `shared_with_me=True` to any search to include shared memories from other
agents alongside your own results. The shared results are blended via the same
RRF fusion pipeline as your local results.

```python
# Typical session-start search
memory_search(query="database migration strategy", shared_with_me=True)

# Filter to shared content on a specific topic
memory_search(query="deployment pipeline", shared_with_me=True, category="decisions")
```

Without `shared_with_me`, the search only looks at your own DB partition.
Shared memories from other agents are invisible.

### `memory_session_start` — Auto-Includes Shared Memories

Every session start already surfaces the latest 5 shared pool entries in its
briefing under **Shared Memories**. You don't need to call anything extra —
the shared context is there from the first turn.

### `memory_profile(action="agents")` — Agent Identity

Confirms your `MEMORY_AGENT_ID`, namespace, and scoped DB. Use this when you
need to verify which agent identity you're operating under, or to discover
which partner agents exist in the shared pool.

## When to Share

| Category | Threshold | What to share |
|----------|-----------|---------------|
| projects | importance >= 3 | Milestones, architecture decisions, system changes |
| decisions | importance >= 3 | Cross-cutting design choices, trade-off records |
| lessons | importance >= 4 | Non-obvious bugs, integration gotchas, hard-won fixes |
| sessions | Never | Too noisy — these are per-session logs |

**Heuristic**: If another agent might waste time re-deriving this, share it.
If it's only useful within your current session, don't.

### Auto-Share

Cron automatically shares your high-importance content (importance >= 4,
fitness >= 0.6, excluding sessions and tests). Manual `memory_share` is still
recommended for context-rich saves at lower thresholds that another agent
would benefit from.

## Sync Daemons

Two background HTTP daemons keep agent DBs in sync:

| Daemon | Port | Manages |
|--------|------|---------|
| OPENCODE sync | 9878 | Main DB |
| MIMOCODE sync | 9877 | Agent-b (MIMOCODE) DB |

Check health:

```
memory_advanced(operation="heartbeat")
```

## Session Protocol

1. **Start** → `memory_session_start` — loads your context + shared memories.
2. **Search** → `memory_search(shared_with_me=True)` when the topic spans
   agent boundaries.
3. **Save** → `memory_save` — auto-shares if importance/fitness thresholds
   are met.
4. **Share** → `memory_share(action="share", ...)` for saves below auto-share
   threshold that another agent needs.
5. **Import** → `memory_share(action="import", ...)` to pull a shared memory
   into your own DB for reinforcement.
6. **End** → `memory_save(category="sessions")` — session-end hook runs
   reinforcement.

## Troubleshooting

- **Shared memory not appearing?** Check `memory_share(action="list")` to
  confirm it's in the pool. If not, call `action="share"`.
- **Agent identity wrong?** Confirm `MEMORY_AGENT_ID` env var. Check
  `memory_profile(action="agents")`.
- **Pool too noisy?** The pool is periodically cleaned of entries below
  importance 3 and older than 90 days.
- **Auto-share not triggering?** Check that importance >= 4 and fitness >= 0.6,
  and category is not sessions or tests.
