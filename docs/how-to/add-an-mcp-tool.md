# How to Add an MCP Tool

Add a new tool to the agentic-memory MCP server. There are 107 tools today (17 CORE + 87 ADMIN + 3 DEPRECATED); this walks you through adding a 108th.

This is the **maintainer** version. For the high-level skill, see `skills/add-an-mcp-tool/SKILL.md`.

## When to use this

- You need to expose a new verb to the agent (e.g., a new read op, a new write op).
- You need to add a new maintenance operation.

## When NOT to use this

- You need a new background job (use `add-a-cron-job`).
- You need a new lifecycle hook (use `add-a-claude-code-hook`).
- You need a one-shot CLI tool (just write `your_tool.py` and add to `docs/how-to/`).

## Decide: CORE or ADMIN?

**CORE** = user-facing verb the agent invokes in normal conversation. There are 17 today (authoritative: `tool_registry.py` `CORE_TOOLS`):
`memory_search`, `memory_save`, `memory_delete`, `memory_recall`, `memory_note`, `memory_learn`, `memory_audit`, `memory_organize`, `memory_share`, `memory_graph`, `memory_profile`, `memory_session_start`, `memory_advanced`, `memory_review_beliefs`, `memory_curate_autosave`, `memory_health_check`, `memory_system_health`.

**ADMIN** = grouped under `memory_maintenance(operation="...")`. There are 87 today (authoritative: `tool_registry.py` `ADMIN_TOOLS`). See `docs/reference/mcp-tools.md` for the full list.

**Rule of thumb:**
- Will the user-agent name this verb? → CORE
- Is this a noun-modifying operation? → ADMIN

If unsure, **add as ADMIN** first. Promote later.

## Add an ADMIN operation (the common case)

1. Open `mcp_maintenance_ops.py`. Add a new entry to the `MAINTENANCE_HANDLERS` dict, matching the existing pattern:

```python
MAINTENANCE_HANDLERS: dict[str, Callable] = {
    # ... existing handlers ...
    "your_op": lambda operation="your_op", **kwargs: _your_op_impl(**kwargs),
}
```

2. Implement `_your_op_impl` in `mcp_maintenance_ops.py`.

3. Add the operation name to the `MaintenanceOp` enum in `mcp_maintenance.py` (so the router lists it in help output).

4. Add a test in `eval/test_mcp_tools.py` (or a new `eval/test_your_op.py`).

## Add a CORE tool (less common)

1. Open an `mcp_*.py` file (e.g. `mcp_memory.py`).
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
venv/bin/python ~/.opencode/scripts/tool_drift_check.py

# 2. Full test
venv/bin/python -m pytest eval/ -q
```

## Reference

- All 107 tools: `tool_registry.py`
- Tool registry: `tool_registry.py`
- Drift check: `~/.opencode/scripts/tool_drift_check.py`
- Skill (deeper version): `skills/add-an-mcp-tool/SKILL.md`
