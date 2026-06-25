---
name: add-an-mcp-tool
description: Maintainer procedure for adding a new MCP tool to the agentic-memory system. Use when the user-facing tool surface needs a new verb (e.g., a new read op, a new maintenance op, a new admin op). Don't use for adding CLI scripts (that's `add-a-cron-job`) or new Claude Code lifecycle hooks (that's `add-a-claude-code-hook`).
---

# Add an MCP Tool

How to add a new tool to the agentic-memory MCP server. There are 70 tools today; this is the canonical way to add a 71st.

## The 60-second version

1. Implement the function in the appropriate `mcp_*.py` module (search, kg, safety, retention, profile, etc.) or in `mcp_maintenance.py` for admin ops.
2. Decorate with `@mcp.tool()`.
3. If it's a CORE tool, add it to `tool_registry.CORE_TOOLS`. If it's an ADMIN op, route it through `memory_maintenance(operation=...)` by adding a handler in `mcp_maintenance_ops.py`.
4. Re-export the new function from `mcp_tools.py` (the canonical tool inventory).
5. Add a test in `eval/test_*.py` (match the existing patterns).
6. Run `scripts/tool_drift_check.py` to verify registration consistency.
7. Run `pytest eval/ -q` to verify no regressions.

Total: ~30 minutes for a simple read tool, ~2 hours for a complex write tool.

## Step 1: decide CORE vs ADMIN

**CORE tools** (always exposed, 15 user-facing verbs):
- Read: `memory_search`, `memory_semantic_search`, `memory_recall_context`, `memory_facts_search`, `memory_graph_search`, `memory_session_start`, `memory_user_profile`
- Write: `memory_save`, `memory_supersede`, `memory_delete`, `memory_restore`
- Safety/admin: `memory_check_contradictions`, `memory_scan_injection`, `memory_rebuild`, `memory_profile_access`

**ADMIN tools** (55 total, grouped under `memory_maintenance(operation=...)`; 9 added 2026-06-22):
- All maintenance ops: heartbeat, compact, consolidate, backfill_all, rebuild, check_integrity, purge_expired, duplicates, merge_suggestions, rewrite_links, tier_stats, review_schedule, pinned_decay, retention_stats, profile_stats, auto_summarize, summarize, daily_digest, compile_skill, share, shared_list, shared_stats, strip_provenance, trash, restore, etc.

**Rule of thumb:**
- Does the user-agent invoke this in normal conversation? → CORE
- Does only the maintainer / dev-tools / cron invoke this? → ADMIN
- Is it a verb-noun the user can name (`memory_save`, `memory_search`)? → CORE
- Is it a noun-modifying operation (`backfill_all`, `consolidate`, `purge`)? → ADMIN

If you're unsure, **add it as ADMIN** with `memory_maintenance(operation="your_op")`. You can promote to CORE later if it gets heavy use.

## Step 2: implement the function

**If ADMIN op (the common case):**

Add the operation as a new handler in `mcp_maintenance_ops.py` (the dispatch table) and a `@mcp.tool()` definition in `mcp_maintenance.py` (the thin wrapper). The router at `mcp_maintenance.memory_maintenance(operation="...")` dispatches to the right handler via `MAINTENANCE_HANDLERS`. There are ~50 ops already; yours is the ~51st.

If your tool lives in a domain other than `mcp_maintenance.py` (e.g., `mcp_sharing.py` for `memory_auto_share`, or `mcp_ctr_drift.py` for `memory_list_drift_alarms`), add the `@mcp.tool()` directly in the domain module, then re-export from `mcp_tools.py`.

```python
# In mcp_maintenance.py:
@mcp.tool()
@with_audit("memory_your_op")
def memory_your_op(arg1: str, arg2: int = 5) -> str:
    """One-line description of what this does."""
    return _your_op_impl(arg1=arg1, arg2=arg2)

# In mcp_maintenance_ops.py:
MaintenanceOp.YOUR_OP = "your_op"  # add to the enum
MAINTENANCE_HANDLERS[MaintenanceOp.YOUR_OP] = lambda *, arg1, arg2, **_: memory_your_op(arg1=arg1, arg2=arg2)
```

**If CORE tool (less common):**

Add a new `@mcp.tool()` function in the appropriate `mcp_*.py` module. The signature should be:

```python
@mcp.tool()
def memory_your_op(arg1: str, arg2: int = 5) -> str:
    """One-line description of what this does.

    Use this when [trigger condition]. Returns [shape].

    Args:
        arg1: what it is
        arg2: what it is (default 5)
    """
    # implementation
```

Then add `"memory_your_op"` to `tool_registry.CORE_TOOLS`.

**Conventions:**
- Return `str`, not dict/list. MCP tools are string-returning. JSON-encode if needed.
- Pull from `memory_common.connection_pool` for connections.
- Use `safe_close_db(conn)` for connection cleanup.
- No `print()` — use `logger.info/warning/error`.
- Don't bypass the saga. If you write to the DB, wrap in `with conn:`.

## Step 3: register the tool

**For CORE:** add to `tool_registry.CORE_TOOLS` (in `tool_registry.py`):

```python
CORE_TOOLS = [
    # ...existing 15...
    "memory_your_op",  # ← add here
]
```

**For ADMIN:** no registry change needed (it's grouped under `memory_maintenance`).

## Step 4: add a test

Create or extend a test file in `eval/`. Match the existing patterns:

```python
# eval/test_your_op.py
import tempfile, sqlite3
import pytest
from mcp_tools import memory_your_op, memory_maintenance

class TestYourOp:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = f"{self.tmpdir}/test.db"
        # run schema setup here

    def teardown_method(self):
        # cleanup
        pass

    def test_your_op_happy_path(self):
        result = memory_your_op(arg1="hello", db_path=self.db_path)
        assert "expected substring" in result

    def test_your_op_error_path(self):
        with pytest.raises(SomeExpectedError):
            memory_your_op(arg1="", db_path=self.db_path)
```

If the test hits the production DB, use the `_ProdDBGuarded` mixin from `eval/test_safety_wiring.py:60-109`.

## Step 5: verify with tool drift check

```bash
venv/bin/python /Users/arka/.opencode/scripts/tool_drift_check.py
```

This catches:
- Tools defined in code but missing from `CORE_TOOLS`/`ADMIN_TOOLS`
- Tools in registry but not actually defined
- Phantom tools (registry entries with no implementation)

## Step 6: full test run

```bash
venv/bin/python -m pytest eval/ -q
```

Should pass with no new failures. If you broke a test, fix it before committing.

## Step 7: update `memory_workflow.md`

If you added a CORE tool, add a row to the "File Locations" table or wherever tool listings live. If you added an ADMIN op, add a row to the `memory_maintenance` operations table.

## Step 8: commit

Commit message format: `[mcp-tool] add memory_your_op for <use case>`

Reference the audit item if it addresses one: `[mcp-tool] add memory_your_op for H5 fix: ...`

## Common pitfalls

- **Don't bypass the saga.** If you write to `memories`, wrap in `with conn:` + `safe_close_db(conn)`. The C1 fix is fragile.
- **Don't add to CORE_TOOLS without checking the tool list.** It's curated. The 15 CORE tools are the day-to-day user verbs.
- **Don't return non-string.** MCP tools are string-returning. JSON-encode if needed (`json.dumps(result)`).
- **Don't write to STDERR from a hook.** The proactive-context hook was a 2-day bug because of this. STDOUT is the channel.
- **Don't forget the tool_registry drift check.** It's a CI step that catches registration drift.

## What if my tool needs to do something not yet supported?

| Need | Where to add it |
|---|---|
| Read from a new table | Add to the read path (`search_pipeline.py`) |
| Write to a new table | Add a migration first (`db_migrations.py`), then the write path |
| Hook into agent lifecycle | Different skill — `add-a-claude-code-hook` |
| Schedule periodically | Different skill — `add-a-cron-job` |
| New embedding model | Different concern — `embedding_search.py` MODEL_ID |

## Reference

- All 70 existing tools: `mcp_tools.py` (re-exports) + `mcp_*.py` (definitions)
- Tool registry: `tool_registry.py` (CORE_TOOLS, ADMIN_TOOLS)
- Drift check: `/Users/arka/.opencode/scripts/tool_drift_check.py`
- Test patterns: `eval/test_*.py` (142 files)
- MCP server: `memory_mcp.py`
- FastMCP docs: https://github.com/jlowin/fastmcp

— last reviewed 2026-06-22
