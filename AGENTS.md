# AGENTS.md — Agentic Memory System (Maintainer Edition)

You are an agent working on the **agentic-memory** codebase at the repo root. This file is your operating contract.

> If you are an agent that *uses* this memory system (working in another project with agentic-memory installed), you want the user-facing skill instead:
> `~/.opencode/skills/agentic-memory/SKILL.md`

---

## What This System Is

Local-first, MCP-server-shaped memory layer for AI agents. All data at `~/.config/agentic-memory/memory/`.

- **Surface**: 15 CORE verbs + `memory_maintenance` router (84 ADMIN + 3 DEPRECATED behind router) + 7 lifecycle hooks + 36 cron scripts + 11 CLI commands
- **Schema**: v30, ~60 tables
- **Code**: ~175k LOC (production + test); see `docs/architecture.md`
- **MCP Help**: `docs/MCP_SURFACE.md` — quick-reference for agents using MCP tools. Read it whenever you need to call an MCP tool and aren't sure which one or how.

---

## Reliability Rules

| # | When | Action |
|---|---|---|
| 1 | Session start | `agentic-memory_memory_session_start(query="<subsystem>")` |
| 2 | Before designing a feature | `agentic-memory_memory_search(query="<feature> <subsystem> design rationale")` |
| 3 | After solving a bug/arch decision | `agentic-memory_memory_save(category="lessons" or "decisions")` |
| 4 | After finding a flaky test | `agentic-memory_memory_save(category="lessons", tags=["flaky"], pinned=true)` |
| 5 | After any index rebuild/cron | `agentic-memory_memory_maintenance(operation="auto_save_status")` |
| 6 | Before pushing write-path code | `agentic-memory_memory_search(query="save_pipeline saga transaction safety")` |
| 7 | Before ending session | `agentic-memory_memory_save(category="sessions")` |
| 8 | After large file ops | `token-optimizer_optimize_session` |
| 9 | .md/DB drift | `python memory_integrity.py <db> --recover-orphan-files` |
| 10 | KG/backlinks orphans | `python memory_integrity.py <db> --repair-kg-orphans` |
| 11 | Auto-save history | `agentic-memory_memory_circuit_breaker_status()` |
| 12 | Temporal KG misbehaving | Set `MEMORY_TEMPORAL_KG=0` |
| 13 | Every significant milestone or decision | `memory_save` a **context-rich periodic note** — captures goal, approach, rationale, improvements, semantic relationships. Not a timestamped log line: it should carry enough context to be useful weeks later. Category: `projects`. Tags: include decision/subsystem context. Importance: 4. |

Minimum: do #1, #7, and #13. Run #8 opportunistically. Use `memory_maintenance(operation="compliance_check")` to audit.

---

## Critical Path

```
agentic-memory/
├── save_pipeline.py + save/    ← write path (saga, FTS5, embeddings, KG, audit)
├── search_pipeline.py + search/ ← read path (FTS5 + usearch + KG fusion)
├── mcp_maintenance.py           ← admin tools + memory_maintenance router
├── tool_registry.py             ← 15 CORE + 84 ADMIN + 3 DEPRECATED (single source of truth)
├── plugin/
│   ├── index.ts                 ← OpenCode adapter (event → TS handler)
│   └── agentic-memory-hooks.ts ← hook implementations (TS → Python subprocess)
├── hooks/                       ← 7 lifecycle hooks + 1 log helper
├── background/
│   ├── auto_save.py             ← async inbox+daemon entry point
│   ├── inbox.py                 ← inbox management + daemon lifecycle
│   ├── daemon.py                ← long-lived inbox drainer
│   ├── tool_complete.py         ← hook → save_memory pipeline
│   └── circuit_breaker.py       ← auto-save failure gating
├── cron/                        ← 36 scheduled jobs + install_crontab.sh
├── mcp_*.py (28 modules)        ← domain-split MCP tools
├── memory/                      ← live store (gitignored)
├── docs/MCP_SURFACE.md          ← MCP tool reference for agents
└── eval/                        ← 233 test files, 4000+ test functions
```

---

## Hard Rules

1. **All writes go through `save_memory`** (`save_pipeline.save_memory`). Hooks and auto-save delegate to it. Don't re-implement.
2. **Connection pool is per-DB-path.** `connection_pool.get(str(db_path))` returns stale connections if the path doesn't exist. Active connections cannot be evicted.
3. **Vec keys/index drift after warm-up.** Run `rebuild_vec_index.py` after warm-up chains, not before.
4. **Schema migrations go in `migrations/NNN_name.sql` + `NNN_name.down.sql`.** Bump `SCHEMA_VERSION` in `migration_runner.py`. Current: **30**. Never edit live DB schema by hand.
5. **Default search is `include_global=True`** with blended RRF. Don't override "for safety."
6. **15 CORE tools are user-facing**; 84 ADMIN under `memory_maintenance(operation=...)`. Don't add CORE tools without checking `docs/MCP_SURFACE.md` first.
7. **Use `--incremental` / `--full` with backfill.** Bare args create 22 MB garbage DBs at repo root.
8. **Tests hitting prod DB must use `_ProdDBGuarded` mixin.** See `eval/test_safety_wiring.py:60-109`.
9. **Lock order: file lock first, then conn.** Both `save_memory` and `_update_memory_index_incremental` follow this order.
10. **Concurrent .md writes preserve losers.** `safe_atomic_write(path, content, expected_existing=...)` saves conflicting on-disk content as `<path>.conflict-<pid>-<ts>`.
11. **CRDT merges write to .md files.** Markdown is the source of truth; stale .md after a merge is silent drift.
12. **Signal handlers installed BEFORE flock check** in `auto_save.py`. Otherwise daemon returns without handlers and ignores SIGTERM.
13. **Cross-process writes are single-writer.** Long-lived daemons hold a `flock`; cron scripts hold per-cron `flock`. Add a `flock` to any new long-lived writer.
14. **Saga rollback cleans up dependent rows.** `save.saga.undo_upsert` calls `save.cleanup.cleanup_memory_relations()` (covers kg_facts, orphan kg_edges, backlinks).
15. **Update docs after code changes.** Stale docs are a maintenance hazard — fix them in the same commit.
16. **Use one persistent worktree for active development.** Reuse it for all ongoing feature work; do not create a new worktree per branch or per commit. Verify security and tests in the worktree before merging to main. Keep worktrees minimal and remove them when no longer needed.
17. **Fix every LSP error in every file you touch.** Every file you read or edit must exit with zero LSP errors (pyright). No `# type: ignore` comments, no `# noqa` for type errors, no silent `except` swallowing of type-correctness issues. Fix the type annotation at the source (function signature, variable declaration) so the error is resolved correctly. Pre-existing errors in files you didn't modify are exempt, but any file you edit must be left fully clean.
18. **Run mypy + ruff before every commit.** Before committing any changes, run `./venv/bin/python -m mypy <any file you modified>` and `./venv/bin/python -m ruff check <any file you modified>`. Fix all errors and warnings. Do not commit with outstanding mypy or ruff issues. This applies even to test files.
19. **Maintenance is automated.** Most maintenance is handled by cron jobs and the background worker (see `cron/install_crontab.sh`). The agent should exercise `memory_organize`, `memory_maintenance`, or individual MCP tools **only when cron is not running or immediate results are needed**. Do not run maintenance tools as a default post-task ritual.

---

## When to Ask vs Act

**Act without asking:**
- Bug fixes where the correct behavior is unambiguous (e.g., fixing a typo, correcting a wrong variable name, fixing a broken import)
- Documentation updates that match existing code behavior or stated intent
- Test additions that lock in existing behavior
- Refactoring that preserves behavior (rename, extract, reorganize)
- Running existing commands in AGENTS.md (mypy, ruff, pytest, memory_integrity)
- Reverting your own changes when you discover they broke something

**Stop and ask the user:**
- Any change to behavior, defaults, or user-facing output
- Architectural decisions (e.g., "should we switch from fire-and-forget to blocking here?")
- Adding or removing features, hooks, cron jobs, MCP tools
- Decisions where multiple valid approaches exist with different tradeoffs
- Anything that costs money, sends data externally, or modifies user data irreversibly
- When you're uncertain which of two or more correct answers is intended

**How to ask:**
Give 2–4 named options with tradeoffs. Do not ask open-ended questions.

Good:
> The daemon spawn path uses `inbox.py` (broken) vs `auto_save.py` (correct). Should I:
> A) Fix to `auto_save.py` now
> B) Leave it and flag for review
> C) Ask you to confirm before changing

Bad:
> What should I do about the daemon?

If you find yourself saying "it depends" or "either approach could work," that's a signal to ask. If the answer is already specified in AGENTS.md, a doc, or a previous decision, act.

---

## Sync Server Security

Binds to `127.0.0.1:9877`. Key env vars: `MEMORY_SYNC_TOKEN` (required), `MEMORY_SYNC_HMAC_SECRET` (optional), `MEMORY_SYNC_TLS_CERT`/`MEMORY_SYNC_TLS_KEY` (native TLS), `MEMORY_SYNC_TLS_CLIENT_CA` (mTLS). Empty `MEMORY_SYNC_CORS_ORIGINS` means no CORS. Non-loopback without TLS logs a warning.

---

---

## MCP Surface Contract

**Source of truth for the MCP tool surface: `docs/MCP_SURFACE.md` + `tool_registry.py`**. The MCP
server exposes **15 CORE tools** directly and hides **84 ADMIN + 3 DEPRECATED** behind
`memory_maintenance(operation="...")`.

| Tier | Count | Access |
|------|-------|--------|
| CORE verbs | 15 | Direct MCP tool call |
| ADMIN (legacy) | 84 | `memory_maintenance(operation="...")` or `memory_advanced(operation="...")` |
| DEPRECATED | 3 | Same as ADMIN (also listed in ADMIN_TOOLS; tracked for audit) |

**When to use which:**
- Use a **CORE verb** if one covers the task (see `docs/MCP_SURFACE.md` for the full verb reference).
- Use `memory_maintenance(operation="...")` for admin/diagnostic ops.
- Never call an ADMIN tool name directly — it will return `ToolError("Unknown tool")` because it is removed
  from the FastMCP surface at startup.

**Tool registry:** `tool_registry.ADMIN_TOOLS` is the `for`-loop target in `memory_mcp.py` (line 231).
Any name in that list should be reachable only via `memory_maintenance`. If you add a new
`@mcp.tool()` in a new `mcp_*.py`, add its name to the correct list in `tool_registry.py` and
update `docs/MCP_SURFACE.md`.

**Entry point:** Always start via `memory_mcp.py` (or `cli.py` which imports it). The `mcp_tools.py`
auto-discovery module is not the server entry point — importing it independently produces a
different tool surface. Do not rely on its auto-registration behavior.

---

## Hook Wiring

OpenCode fires lifecycle events. The **TypeScript plugin** (`plugin/index.ts` adapter + `plugin/agentic-memory-hooks.ts` implementations) receives them and spawns Python scripts as subprocesses. The TS plugin runs its own circuit breaker (10 failures / 5 min cooldown) before spawning.

| OpenCode Event | TS Handler | Python Script | Mode | Purpose |
|---|---|---|---|---|
| `session.created` | `startSession` | `memory-recall-session.py` + `memory-session-start.py` | blocking (Promise.all) | Load session context before first tool |
| `tool.execute.before` | `beforeTool` | `memory-proactive-context.py` | blocking per-call | Search relevant memories before each tool |
| `tool.execute.after` | `onToolAfter` | `context_monitor.py track` + `auto_save.py tool-complete` | fire-and-forget | Record tool call + auto-save |
| `session.idle` | `onIdle` | `context_monitor.py idle` | fire-and-forget | Session idle checkpoint |
| `session.deleted` | `endSession` | `context_monitor.py end` (blocking) + `memory-session-end.py` (blocking, 10s timeout) | mixed | Flush final summary + save session |
| `experimental.chat.system.transform` | `injectSystemPrompt` | N/A (in-process) | inline | Pushes collected context into system prompt |
| `experimental.session.compacting` | `onCompacting` | `memory-precompact-snapshot.py` (blocking) + `context_monitor.py compact` (blocking) | blocking | Snapshot + context save before compaction |

**Key behaviors:**
- **Session context** (`state.sessionContext`): one-time bootstrap from recall + session-start. Injected once at session creation, then cleared.
- **Proactive context** (`state.proactiveContext`): per-tool search result from `beforeTool`. Injected into the next LLM call via `experimental.chat.system.transform`, then cleared. The agent always sees the latest proactive context via stdout on every tool call.
- **Fire-and-forget** (`tool.execute.after`, `session.idle`): no completion signal to the agent. Failures go to `hook-errors.jsonl` + `hooks.log`.
- **Blocking** (`session.created`, `tool.execute.before`, `session.deleted`, `experimental.session.compacting`): agent waits. Errors are surfaced via the TS circuit breaker log callback.
- **Python hooks** (`hooks/*.py`) are standalone scripts — they read JSON from stdin or CLI args, write results to stdout, and handle all exceptions internally (`except BaseException → log_error() → sys.exit(0)`). They never crash the agent.

See `opencode.jsonc` for plugin registration. The TS plugin is the single wiring layer — don't call Python hooks directly unless debugging.

---

## Feature Flags

| Flag | Default | Notes |
||---|---|
| `MEMORY_TEMPORAL_KG` | ON | Event-time extraction, contradiction detection, supersession. Set `0` to disable if false contradictions or edit invalidation are too aggressive. `kg_facts.locked = 1` prevents per-fact supersession. |

See `memory.toml` for all 17 feature flags.

---

## Skills

| Skill | Trigger |
|---|---|
| `skills/memory-architecture/` | "How does the system work?" |
| `skills/add-an-mcp-tool/` | "Add a new MCP tool" |
| `skills/add-a-cron-job/` | "Add a new cron job" |
| `skills/add-a-claude-code-hook/` | "Add a new lifecycle hook" |

---

## Emergency

1. **Stale lock — diagnose first.** Both `.rebuild.lock` and `.vec_rebuild.lock` use `fcntl.flock`, which auto-releases when the holding process dies. An empty lock file on disk alone is not a real contention — the actual protection is the OS-level flock held by an open FD. Before removing anything: run `ps aux | grep python` and try a non-blocking acquire yourself (the next legitimate writer will succeed automatically if no live process holds it). If a live process IS holding the lock, find it with `lsof | grep rebuild.lock` and decide whether to wait or kill it. **Never `rm` a lock that a live process is holding** — it will corrupt the write it's mid-way through. If the holder is dead (no matching PID), the empty file is safe to remove: `rm memory/.rebuild.lock`.
2. Check audit log: search for `error`, `crash`, `orphan`, `drift`.
3. Check cron logs: `memory/worker.log`, `memory/heartbeat.log`, `memory/integrity.log`.
4. Run integrity check: `venv/bin/python memory_integrity.py memory/memory.db`. 0 critical = OK.
5. Stuck? Read `eval/test_*.py` for the regression net.

---

## Current Status (2026-07-05 snapshot)

- **Schema v30**: 30 migrations applied (100% down-migration coverage). Chunk-level multi-vector search active.
- **Phase 1 (Docs/Drift)**: `tool_drift_check.py`, `doc_drift_check.py`, `schema_version_check.py` in CI. Tool count reconciled: 102 total (15 CORE + 84 ADMIN + 3 DEPRECATED; 15 verbs surfaced directly including `memory_curate_autosave`).
- **Phase 2 (Search Observability)**: 6 search phases instrumented with `infra.error_counter`. Failures return `<call>_phase_inc("<phase>", e)` + `logger.warning`. `search_memories` adds `phase_errors` to result envelope when counter is non-empty.
- **Phase 1 tools**: `memory_flags_status`, `memory_phase_errors` admin ops added.
- **Circuit-breaker fixed**: 5 handler lambda signatures corrected.
- **Tool surface cleanup**: `mcp_kg.py` orphans (`memory_graph_insights`, `memory_graph_evolution`) added to `ADMIN_TOOLS`. Silent `remove_tool` failures now log a warning. Bulk-removal loop in `memory_mcp.py` reinforced with explicit `mcp_kg` and `mcp_maintenance` pre-imports.
- **best_dist wired**: `_late_interaction_score` and `_late_interaction_score_batch` now return `(score, avg_best_dist)`. Positional coherence surfaced as 12th tuple element in late-interaction rerank results. Cross-encoder rerank also carries 12th element (None) for consistent shape.
- **Rule enforcement**: `memory-session-end.py` (Rule #7), `cron_health_check.py` (Rules #5, #9-11), `memory_compliance_check` MCP tool.
- **Plugin wiring**: `plugin/index.ts` (OpenCode adapter) + `plugin/agentic-memory-hooks.ts` (hook implementations). 7 lifecycle event handlers with TS-level circuit breaker.
- **Daemon fix**: `_start_daemon_if_needed()` now correctly spawns `auto_save.py daemon` (was incorrectly spawning `inbox.py`). Daemon auto-spawns on first auto-save hook call.
- **Contextual retrieval symmetry**: Query embeddings now receive the same `[category|tags]` prefix as document embeddings when `MEMORY_CONTEXTUAL_RETRIEVAL=1`.
- **Cron**: 36 scheduled jobs. `background_worker` every 15 min with flock protection.
- **Auto-save**: Async inbox+daemon (2-5ms enqueue). Default since 2026-06-22.
- **Deferred indexing**: MCP `memory_save` defers embedding/KG/facts to background worker — returns <200ms, never times out.
- **MCP reference doc**: `docs/MCP_SURFACE.md` — complete verb reference, decision tree, admin ops table, common workflows.
- **Mypy**: 0 errors. **Coverage**: 70% gate.
- **Tests**: 233 test files, 4000+ test functions.
- **Test command**: `./venv/bin/python -m pytest eval/ --timeout=15 -q` (in-process runner) **|** Full suite with xdist parallelism: `./venv/bin/python -m pytest eval/ -n 3 --timeout=15 -q` **|** Subprocess-per-file runner (avoids parallel torch/OpenMP crashes): `./venv/bin/python eval/run_full_suite.py`

> Note: Current Status is a point-in-time snapshot. It will drift. For authoritative counts, query the codebase directly.
