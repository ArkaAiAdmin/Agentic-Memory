# How to Add an MCP Tool

Add a new tool to the agentic-memory MCP server. There are 56 tools today; this walks you through adding a 57th.

This is the **maintainer** version. For the high-level skill, see `skills/add-an-mcp-tool/SKILL.md`.

## When to use this

- You need to expose a new verb to the agent (e.g., a new read op, a new write op).
- You need to add a new maintenance operation.

## When NOT to use this

- You need a new background job (use `add-a-cron-job`).
- You need a new lifecycle hook (use `add-a-claude-code-hook`).
- You need a one-shot CLI tool (just write `your_tool.py` and add to `docs/how-to/`).

## Decide: CORE or ADMIN?

**CORE** = user-facing verb the agent invokes in normal conversation. There are 18 today:
`memory_search`, `memory_recall_context`, `memory_session_start`, `memory_save`, `memory_delete`, `memory_restore`, `memory_supersede`, `memory_reinforce`, `memory_facts_search`, `memory_graph_search`, `memory_check_contradictions`, `memory_scan_injection`, `memory_rebuild`, `memory_audit`, `memory_compile_skill`, `memory_facts_stats`, `memory_graph_stats`, `memory_quality_filter`.

**ADMIN** = grouped under `memory_maintenance(operation="...")`. There are 37+ today: heartbeat, compact, consolidate, backfill_all, check_integrity, duplicates, merge_suggestions, rewrite_links, tier_stats, review_schedule, pinned_decay_check, retention_stats, profile_stats, profile_access, auto_summarize, summarize, daily_digest, share, shared_list, shared_stats, etc.

**Rule of thumb:**
- Will the user-agent name this verb? → CORE
- Is this a noun-modifying operation? → ADMIN

If unsure, **add as ADMIN** first. Promote later.

## Add an ADMIN operation (the common case)

1. Open `mcp_tools.py`. Find the `memory_maintenance()` function.
2. Add a new dispatch branch to the if/elif chain. Match the existing pattern:

```python
@mcp.tool()
def memory_maintenance(operation: str, db_path: str = "", **kwargs) -> str:
    """All admin/maintenance operations."""
    # ... existing branches ...
    elif operation == "your_op":
        return _your_op_impl(db_path=db_path, **kwargs)
    # ...
```

3. Implement `_your_op_impl` somewhere in `mcp_tools.py` (or import it).

4. Add it to the operations table in the `memory_maintenance` docstring (so the agent knows it exists).

5. Add a test in `eval/test_mcp_tools.py` (or a new `eval/test_your_op.py`).

## Add a CORE tool (less common)

1. Open `mcp_tools.py`.
2. Add a new function with `@mcp.tool()` decorator:

```python
@mcp.tool()
def memory_your_op(arg1: str, arg2: int = 5, db_path: str = "") -> str:
    """One-line description.

    Use this when [trigger]. Returns [shape].
    """
    # implementation
```

3. Add `"memory_your_op"` to `CORE_TOOLS` in `tool_registry.py`.

4. Add a test.

## Conventions

- Return `str`, not dict/list. JSON-encode if needed.
- Use `db_path: str = ""` parameter; resolve via `memory_common.get_memory_paths()`.
- Use `safe_close_db(conn)` for connection cleanup.
- No `print()` — use `logger.info/warning/error`.
- Don't bypass the saga. If you write to the DB, wrap in `with conn:`.

## Verify

```bash
# 1. Drift check
venv/bin/python /Users/arka/.opencode/scripts/tool_drift_check.py

# 2. Full test
venv/bin/python -m pytest eval/ -q
```

## Reference

- All 56 tools: `mcp_tools.py`
- Tool registry: `tool_registry.py`
- Drift check: `/Users/arka/.opencode/scripts/tool_drift_check.py`
- Skill (deeper version): `skills/add-an-mcp-tool/SKILL.md`
