# How to Add an MCP Tool

## Goal

Add a new tool to the agentic-memory MCP server — either a CORE user-facing verb or an ADMIN maintenance operation.

## Prerequisites

- [ ] Python 3.10+
- [ ] Familiarity with the MCP tool surface (`docs/MCP_SURFACE.md`)
- [ ] Access to `mcp_maintenance_ops.py` (for ADMIN ops) or an `mcp_*.py` file (for CORE tools)
- [ ] Read the tool registry: `tool_registry.py` (CORE_TOOLS/ADMIN_TOOLS lists)

There are 107 tools today (17 CORE + 87 ADMIN + 3 DEPRECATED); this walks you through adding a 108th.

This is the **maintainer** version. For the high-level skill, see `skills/add-an-mcp-tool/SKILL.md`.

## When to use this

- You need to expose a new verb to the agent (e.g., a new read op, a new write op).
- You need to add a new maintenance operation.

## When NOT to use this

- You need a new background job (use `add-a-cron-job`).
- You need a new lifecycle hook (use `add-a-claude-code-hook`).
- You need a one-shot CLI tool (just write `your_tool.py` and add to `docs/how-to/`).

## Steps

### 1. Decide: CORE or ADMIN?

**CORE** = user-facing verb the agent invokes in normal conversation. There are 25 today (authoritative: `tool_registry.py` `CORE_TOOLS`):
`memory_search`, `memory_save`, `memory_delete`, `memory_recall`, `memory_note`, `memory_learn`, `memory_audit`, `memory_organize`, `memory_share`, `memory_graph`, `memory_profile`, `memory_session_start`, `memory_session_end`, `memory_recall_context`, `memory_review_beliefs`, `memory_curate_autosave`, `memory_health_check`, `memory_system_health`, `memory_advanced`, `memory_record_ctr_feedback`, `memory_coordinate`, `memory_list_skills`, `memory_extract_skills`, `memory_compile_skill`, `memory_list_revisions`.

**ADMIN** = grouped under `memory_maintenance(operation="...")` — reachable by agents only via `memory_advanced`. There are 92 today (authoritative: `tool_registry.py` `ADMIN_TOOLS`). See `docs/reference/mcp-tools.md` for the full list.

**Rule of thumb:**
- Will the user-agent name this verb? → CORE
- Is this a noun-modifying operation? → ADMIN

If unsure, **add as ADMIN** first. Promote later.

### 2. Add an ADMIN operation (the common case)

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

### 3. Add a CORE tool (less common)

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

### 4. Conventions

- Return `str`, not dict/list. JSON-encode if needed.
- Use `db_path: str = ""` parameter; resolve via `memory_common.get_memory_paths()`.
- Use `safe_close_db(conn)` for connection cleanup.
- No `print()` — use `logger.info/warning/error`.
- Don't bypass the saga. If you write to the DB, wrap in `with conn:`.

## Verification

```bash
# 1. Drift check
venv/bin/python ~/.opencode/scripts/tool_drift_check.py

# 2. Full test
venv/bin/python -m pytest eval/ -q
```

## Troubleshooting

### Tool not showing up in the agent's tool list

**Cause**: The tool name was not added to `CORE_TOOLS` or `ADMIN_TOOLS` in `tool_registry.py`.
**Fix**: Add the name to the correct list and restart the MCP server.

### ADMIN operation routed to wrong handler

**Cause**: The operation name in `MAINTENANCE_HANDLERS` doesn't match the `MaintenanceOp` enum.
**Fix**: Ensure both entries use the exact same lowercase-with-underscores name.

### Tool drift check fails

**Cause**: A tool was added or removed without updating `tool_registry.py`.
**Fix**: Run `venv/bin/python ~/.opencode/scripts/tool_drift_check.py` to identify the mismatch.

## Related

- All 107 tools: `tool_registry.py`
- Tool registry: `tool_registry.py`
- Drift check: `~/.opencode/scripts/tool_drift_check.py`
- Skill (deeper version): `skills/add-an-mcp-tool/SKILL.md`
- [MCP Tools Reference](../reference/mcp-tools.md) — Full tool catalog
- [Add a Cron Job](add-a-cron-job.md) — For background jobs
