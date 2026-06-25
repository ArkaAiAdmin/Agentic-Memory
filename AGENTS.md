# AGENTS.md — Agentic Memory System (Maintainer Edition)

You are an agent working on the **agentic-memory** codebase at the repo root. This file is your operating contract.

> If you are an agent that *uses* this memory system (working in another project with agentic-memory installed), you want the user-facing skill instead:
> `~/.opencode/skills/agentic-memory/SKILL.md`

---

## What This System Is

Local-first, MCP-server-shaped memory layer for AI agents. All data at `~/.config/agentic-memory/memory/`.

- **Pipeline**: 56,799 LOC Python production (write path, read path, audit pipeline, KG, neural, multi-agent, safety) + 69,155 LOC tests
- **Surface**: 85 MCP tools (15 CORE + 70 ADMIN under `memory_maintenance`) + 4 user-facing hooks + 25 cron scripts / 26 scheduled jobs + 11 CLI commands
- **Schema v21**: ~51 tables total (~31 user-visible: 28 domain tables + 3 FTS virtual tables; ~20 FTS internals)
- Full arch map: `docs/architecture.md` · System ref: `memory_workflow.md`

---

## 8 Things You Must Do

| # | When | Action | Why |
|---|---|---|---|---|
| 1 | Session start, before reading code | `agentic-memory_memory_session_start(query="<subsystem>")` | Pulls pinned notes, recent digests |
| 2 | Before designing any new feature | `agentic-memory_memory_search(query="<feature> <subsystem> design rationale")` | System redesigned 5+ times — don't re-debate |
| 3 | After solving a non-obvious bug or making an architectural call | `agentic-memory_memory_save(category="lessons" or "decisions")` | Audit pipeline depends on lesson notes |
| 4 | After running tests and finding a flaky test | `agentic-memory_memory_save(category="lessons", tags=["flaky"], pinned=true)` | Makes triage easier |
| 5 | After running any index rebuild / cron / maintenance | `agentic-memory_memory_maintenance(operation="auto_save_status")` | Confirms it's healthy |
| 6 | Before pushing code that touches the write path | `agentic-memory_memory_search(query="save_pipeline saga transaction safety")` | Saga is fragile |
| 7 | Before ending session | `agentic-memory_memory_save(category="sessions")` | Next-session recovery |
| 8 | After large file operations | `token-optimizer_optimize_session` | Compresses cached file ops |
| 9 | When investigating .md/DB drift | `./venv/bin/python memory_integrity.py <db> --recover-orphan-files` (or `--repair-fts-drift`) | New in 2026-06-22: silent drift auto-healing |
| 10 | When investigating KG/backlinks orphans | `./venv/bin/python memory_integrity.py <db> --repair-kg-orphans` | New in 2026-06-22 follow-up (B-3 fix): saga rollbacks and pre-fix hard_delete_note calls can leave orphans |
| 11 | When investigating auto-save breaker history | `agentic-memory_memory_circuit_breaker_status()` | New in 2026-06-22 follow-up: open/close events persisted to memory_audit_log |
| 12 | When the fact-level temporal KG is misbehaving (false contradictions, edit invalidation wrong) | Set `MEMORY_TEMPORAL_KG=0` to disable | T8: flag-gated escape hatch — reverts to plain fact extraction with no temporal logic |

Minimum: do #1 and #7. Run #8 opportunistically after large writes.

---

## File Layout (Key Directories)

```
agentic-memory/
├── AGENTS.md                    ← you are here
├── memory_workflow.md           ← system reference: arch, pipelines, hooks, config, troubleshooting
├── memory.toml                  ← centralized feature flags + tuning (env var overridable)
├── config.py                    ← config singleton dataclass
├── skills/                      ← invokable maintainer skills (architecture, add-tool, add-cron, add-hook)
├── docs/                        ← user-facing docs (architecture, how-to, concepts, explanation, reference)
├── hooks/                       ← 4 user-facing hooks + 1 log helper module
├── memory/                      ← live store (gitignored)
├── eval/                        ← 183 test files, 3,494 test functions
├── cron/                        ← 25 background jobs + install_crontab.sh
├── save_pipeline.py + save/     ← write path (save_pipeline: ~1,623 LOC shim; save/: ~1,400 LOC, 5 submodules)
├── search_pipeline.py + search/ ← read path (search_pipeline: shim; search/orchestrator.py: 1,995 LOC; search/: ~4,500 LOC, 8 submodules)
├── backfill_all.py + backfill/  ← audit pipeline (backfill_all: 816 LOC; backfill/: ~1,100 LOC)
├── mcp_*.py (26 modules)        ← domain-split MCP tools (17 domain + 8 support)
├── memory_mcp.py                ← thin orchestrator, delegates to domain modules
├── mcp_maintenance.py + mcp_maintenance_ops.py ← admin tools + dispatch table (~50 ops)
├── tool_registry.py             ← 15 CORE + 70 ADMIN tools (single source of truth)
└── sync_server.py / sync_client.py ← multi-agent sync with native TLS + mTLS
```

---

## Hard Rules (Will Bite You)

1. **All write paths go through `save_pipeline.save_memory`.** The 3 hooks are read-only; auto-save delegates to `save_memory`. Don't re-implement.
2. **Connection pool is per-DB-path.** `connection_pool.get(str(db_path))` returns stale connection if path doesn't exist. Guard with `db_path.exists()`. **Active conns (depth > 0) cannot be evicted** — `_evict_lru` skips them and raises `PoolExhaustedError` if all are active (P0-3 fix 2026-06-22).
3. **Vec_keys/vec_idx drift after warm-up.** Run `rebuild_vec_index.py` after warm-up chains, not before. If FTS5 drifts from `memories`, run `python memory_integrity.py <db> --repair-fts-drift` (Scenario 11 fix 2026-06-22).
4. **Schema migrations go in `migrations/NNN_name.sql` + `NNN_name.down.sql`.**     Bump `SCHEMA_VERSION` in `migration_runner.py`. Current: **21** (v13 field_crdt, v14 arc, v15 drift_alarms, v16 concept_drift, v17 kg_cascade, v18 fact_temporal, v19 kg_facts entity FKs, v20 kg_facts FTS5, v21 kg_crdt). Never edit live DB schema by hand.
5. **`agentic-memory_memory_search` default is `include_global=True` with blended RRF.** Don't override "for safety."
6. **15 CORE tools are user-facing; 70 ADMIN tools under `memory_maintenance(operation=...)`.** Don't add CORE tools without checking.
7. **Use `--incremental` / `--full` flags with backfill.** Bare args create 22 MB garbage DBs at repo root.
8. **Tests hitting prod DB must use `_ProdDBGuarded` mixin.** See `eval/test_safety_wiring.py:60-109`.
9. **Lock acquisition order: file lock first, then conn** (P0-2 fix 2026-06-22). Both `save_memory` and `_update_memory_index_incremental` follow this order. The saga supports a `lock_already_held` kwarg so it doesn't double-acquire.
10. **Concurrent .md writes preserve losers.** `safe_atomic_write(path, content, expected_existing=...)` saves the on-disk content as `<path>.conflict-<pid>-<ts>` if it differs from expected (Scenario 4 fix 2026-06-22). Use this in any code that writes .md files after a CRDT merge or external edit.
11. **CRDT merges write to .md files.** Every successful `crdt_save` / `crdt_field_save` write the merged content to the .md file via `_write_merged_markdown` / `_finalize_crdt_save` (Remediation #5 fix 2026-06-22). The markdown is the source of truth; a stale .md after a merge is silent drift.
12. **Per-`.md`-file signal handlers in `auto_save.py` are installed BEFORE the flock check.** Otherwise a daemon that fails the flock check returns without handlers and ignores SIGTERM (3 ghost daemons observed 2026-06-22; required SIGKILL to kill).
13. **Cross-process write access is single-writer by convention** (B-3 + 2026-06-22 follow-up audit). The deploy guarantees that no two long-lived processes hold a write transaction on the same DB simultaneously. The long-lived daemons (`auto_save.py daemon`, `background_worker`) hold a `flock`; the cron scripts each hold a per-cron `flock` per `install_crontab.sh`; the MCP tool invocations run inside the opencode process. If you add a new long-lived process that writes to the DB, **you must add a `flock` to it** — the connection pool's per-thread keys are intra-process only.
14. **Saga rollback cleans up dependent rows.** `save.saga.undo_upsert` calls `save.cleanup.cleanup_memory_relations()` (B-3 fix 2026-06-22 follow-up). The helper covers kg_facts, orphan kg_edges, and backlinks. Pre-migration 017 databases did not have the cascade FK, so this is the backstop that closes the audit gap. If you add a new post-save hook that writes dependent rows, add a matching cleanup step in `save/cleanup.py` and call it from the helper.

---

## Sync Server Security Model

The `sync_server.py` HTTP server exposes CRDT sync over the network. Binds to `127.0.0.1:9877` by default.

| Env var | Purpose | Default |
|---------|---------|---------|
| `MEMORY_SYNC_TOKEN` | Bearer token | none (server refuses start) |
| `MEMORY_SYNC_HMAC_SECRET` | HMAC-SHA256 body signature | none (skipped) |
| `MEMORY_SYNC_MAX_AGE` | Reject old timestamps | 300s |
| `MEMORY_SYNC_CORS_ORIGINS` | CORS origins | empty (no CORS header sent) |
| `MEMORY_SYNC_TLS_CERT` / `MEMORY_SYNC_TLS_KEY` | Native TLS | none (plaintext) |
| `MEMORY_SYNC_TLS_CLIENT_CA` | mTLS client CA | none |

**Security posture** (2026-06-22): empty `SYNC_CORS_ORIGINS` means
"no CORS" (browser blocks cross-origin) — the previous
`Access-Control-Allow-Origin: *` fallback was removed (SEC-1
fix).  When bound to a non-loopback address without TLS, a
warning is logged at startup (SEC-4 fix). `_is_loopback` correctly
classifies `0.0.0.0` as non-loopback (it's "all interfaces" — same
exposure as a public IP for security purposes).

Deployment tiers: localhost single-agent → trusted LAN → untrusted network with TLS/mTLS.

---

## Hook Wiring

Two lifecycle hooks in `~/.claude/settings.json` or project-local `.claude/settings.json`:

- `hooks/memory-proactive-context.py` — PreToolUse (reads `MEMORY_HOOK_RESULT_LIMIT`, default 3)
- `hooks/memory-session-start.py` — SessionStart (reads `MEMORY_HOOK_RESULT_LIMIT`, default 5)

Both share a per-process search dedup cache (`MEMORY_HOOK_CACHE_TTL` 300s, `MEMORY_HOOK_CACHE_SIZE` 5). `auto_save.py on_tool_complete` is wired via `opencode.jsonc`, not `settings.json`.

The other files in `hooks/` — `memory-search-on-demand.py` (CLI helper) and `_log_error.py` (logging module) — are NOT lifecycle hooks.

---

## Feature Flags (T8, 2026-06-23)

| Flag | Default | What it does |
|---|---|---|
| `MEMORY_TEMPORAL_KG` (or `feature_temporal_kg` in `[features]`) | **ON** | Fact-level temporal KG: event_time extraction, contradiction detection, supersession, edit invalidation. Set to `0` to disable the entire temporal subsystem. |

**When to disable `MEMORY_TEMPORAL_KG`**:
- False contradictions are superseding facts that shouldn't be (e.g., a memory legitimately contains multiple objects for the same S+P)
- Edit invalidation is too aggressive (removing facts the user didn't intend to remove)
- The temporal subsystem is causing measurable performance issues
- You want to opt out temporarily while debugging

**What disabling does NOT affect**:
- Fact extraction itself still works (regex + LLM)
- `kg_facts` table still gets populated
- All other features (tier migration, search, embeddings, etc.) work normally

**How to verify it's working**:
- `python memory_integrity.py <db> --temporal-summary` shows supersession counts (will be 0 when disabled)
- New facts will have `event_time = NULL` and `event_time_granularity = NULL` when the flag is off

**Per-fact escape hatches** (more targeted than disabling the whole flag):
- `kg_facts.locked = 1` — prevents a fact from being superseded or invalidated
- `kg_facts.contradiction_score < 1.0` — current detector uses 1.0; future versions may allow LLM-scored soft contradictions

---

## How to Invoke Maintainer Skills

| Skill | Trigger |
|---|---|
| `skills/memory-architecture/` | "How does the system work?" |
| `skills/add-an-mcp-tool/` | "Add a new MCP tool" |
| `skills/add-a-cron-job/` | "Add a new cron job" |
| `skills/add-a-claude-code-hook/` | "Add a new lifecycle hook" |

---

## Emergency: When the System Is Broken

1. Most issues are: stale lock file (`rm memory/.rebuild.lock` / `memory/.consolidate.lock`), orphaned vec_keys (`rebuild_vec_index.py`), or schema drift (`migration_runner`).
2. Check audit log: `agentic-memory_memory_search` for `error`, `crash`, `orphan`, `drift`.
3. Check cron logs: `memory/worker.log`, `memory/heartbeat.log`, `memory/integrity.log`.
4. Run integrity check: `venv/bin/python memory_integrity.py memory/memory.db`. 0 critical = OK.
5. Stuck? Read `eval/test_*.py` for the regression net.

---

## Current State (2026-06-24)

- **Circuit-breaker dispatch fix**: 5 lambda wrappers in `mcp_maintenance_ops.py:_get_handlers()` had required keyword-only args for parameters that are optional in the underlying functions, causing `TypeError` on bare `memory_maintenance(operation=...)` calls and tripping the circuit breaker. Fixed handlers: `CHECK_INTEGRITY`, `AUDIT_QUERY`, `CIRCUIT_BREAKER_STATUS`, `TEMPORAL_CONTRADICTIONS`, `LIST_DRIFT_ALARMS`.
- **Schema v21**: kg_crdt tables (`kg_entity_crdt`, `kg_edge_crdt`) added for CRDT multi-agent merge support. Temporal KG (T2–T8) default ON via `MEMORY_TEMPORAL_KG=1` (T8 escape hatch: set to `0` to disable).
- **Enrichment safety confirmed**: `_enrich_context()` has try/except at all 3 levels; caller catches without propagating. Not the circuit-breaker cause.
- **Test command**: `./venv/bin/python -m pytest eval/ -v --tb=short`



- **Cron**: 26 scheduled jobs; install with `bash cron/install_crontab.sh` (idempotent). Note: `background_worker.py` runs `*/5 * * * *` and uses a per-cron flock to prevent concurrent workers.
- **Vec index**: Auto-repaired by `background_worker` every 15 min (now with flock protection as of 2026-06-22)
- **Auto-save**: Async/background-batch path (default since 2026-06-22 session 2). `tool_complete` enqueues to `<memory>/.auto_save_inbox.jsonl`; the `auto_save.py daemon` process tails it and flushes in batches (default: 50 entries or 500ms). Per-call latency drops from ~100-200ms (sync) to ~2-5ms (enqueue). Set `MEMORY_ASYNC_AUTOSAVE=0` to force the sync path.
- **God-function decomposition** (2026-06-22): `save_memory` (216→110 lines), `_run_post_save_hooks` (113→40), `search_memories` (551→244). 23 named helpers extracted, 50 new tests in `test_refactor_helpers.py`.
- **Codebase**: 46,247 LOC top-level `*.py` (excl. `eval/`) + 69,155 LOC in `eval/`. The "56,799" total in the Pipeline line above includes all production files at any depth (`save/`, `search/`, `cron/`, `backfill/`, `hooks/`), not just root-level.
- **Mypy**: 0 errors on 5 core modules
- **Coverage**: 70% gate

## Async Auto-Save (Summary)

The auto-save hook uses an **inbox + daemon** architecture for low-latency enqueue (~2-5ms). `tool_complete` appends a JSONL line to `<memory>/.auto_save_inbox.jsonl`; a long-running `auto_save.py daemon` tails it and flushes in batches (default: 50 entries or 500ms). PID file + flock guarantee single-daemon. Set `MEMORY_ASYNC_AUTOSAVE=0` to force the sync path. Full internals in `docs/architecture.md` and `memory_workflow.md`.

**Tunables**: `AUTO_SAVE_BATCH_INTERVAL=0.5`, `AUTO_SAVE_BATCH_SIZE=50`, `AUTO_SAVE_DAEMON_IDLE_S=3600`, `AUTO_SAVE_INBOX_MAX_BYTES=104857600`.

