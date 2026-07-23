Flat collection of single-purpose `mcp_*.py` files, each a thin `@mcp.tool()`-decorated wrapper around deeper domain logic in sibling packages (`infra`, `search`, `save_pipeline`, `crdt`, `belief`, `agentic_memory`).

- Entry point / registry: `mcp_tools.py` is the package root. It globs `mcp_*.py`, imports them for side-effect registration, then re-exports every `memory_*` symbol plus a small `_extra_exports` set via `__getattr__` + Phase 2 globals population, so adding a new file requires no changes.
- Shared bootstrap: `mcp_common.py` re-exports infrastructure primitives (`ErrorCode`, `_err`, `with_audit`, `open_db`, `GLOBAL_MEM_DIR`, `get_memory_paths`) from `infra.*` and defines `_bootstrap_path` / `_resolve_memory_dir` used by every other module; it is imported first to fix the user-level data root before any tool runs.
- Domain slices (each one file):
  - `mcp_verbs.py`: the canonical 17-agent-surface tools (`memory_search`, `memory_save`, `memory_note`, `memory_learn`, `memory_delete`, `memory_recall`, `memory_review_beliefs`, `memory_curate_autosave`, `memory_organize`, `memory_share`, `memory_system_health`, `memory_advanced`, …). Every verb starts with an RBAC check (`_check_authorization`) and wraps DB calls with `_wrap_db_error`.
  - `mcp_async.py`: four `async_*` wrappers (`async_memory_save/search/save_batch/search_batch`) built on `asyncio.to_thread` around sync `mcp_memory`/`mcp_search` entry points.
  - `mcp_crdt.py`: `memory_crdt_sync` + `memory_crdt_status` — peer CRDT merge and sync-log status.
  - `mcp_ctr_drift.py`: CTR feedback recording and concept-drift alarm management (`drift_alarms` table).
  - `mcp_agent.py`: per-agent context lifecycle (`memory_agent_init/clear/list`) for namespace scoping.
  - `mcp_coordination.py`: multi-agent task queue, file locking, inter-agent messaging, project state — all backed by SQLite tables (`shared_tasks`, `file_locks`, `agent_messages`, `project_state`) accessed through a pooled connection.
  - `mcp_sdk.py`: `memory_sdk_demo` end-to-end demo mirroring the CLI `demo` subcommand.

Dependency direction: `mcp_*` → `mcp_common` → `infra.*`; cross-cutting concerns (RBAC, audit, error envelopes, DB path resolution) are centralized in `mcp_verbs.py` helpers and reused by other modules. No circular imports among the `mcp_*.py` files — they only depend on `mcp_instance.mcp` for decoration and on `mcp_common` for shared helpers.