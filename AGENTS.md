# AGENTS.md — Agentic Memory System (Maintainer Edition)

You are an agent working on the **agentic-memory** codebase at the repo root. This file is your operating contract.

> If you are an agent that *uses* this memory system (working in another project with agentic-memory installed), you want the user-facing skill instead:
> `~/.opencode/skills/agentic-memory/SKILL.md`

---

## What This System Is

Local-first, MCP-server-shaped memory layer for AI agents. All data at `~/.config/agentic-memory/memory/`.

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
| 17 | Dashboard metric shows `—` | `python -c "import sqlite3; c=sqlite3.connect('memory/memory.db'); print(c.execute('SELECT name FROM sqlite_master WHERE type=\\'table\\'').fetchall())"` — check the table gotchas section |

Minimum: do #1 and #7. Run #8 opportunistically. Use `memory_maintenance(operation="compliance_check")` to audit.

---

## Critical Path

```
agentic-memory/
├── save_pipeline.py + save/    ← write path (saga, FTS5, embeddings, KG, audit)
├── search_pipeline.py + search/ ← read path (FTS5 + usearch + KG fusion)
├── mcp_maintenance.py           ← admin tools + memory_maintenance router
├── tool_registry.py             ← 18 CORE + 71 ADMIN (89 total; single source of truth)
├── hooks/                       ← 4 lifecycle hooks + 2 helpers
├── cron/                        ← 27 background jobs
├── mcp_*.py (27 modules)        ← domain-split MCP tools
├── memory/                      ← live store (gitignored)
└── eval/                        ← 200 test files, 3,710 test functions
```

---

## Hard Rules

1. **All writes go through `save_memory`** (`save_pipeline.save_memory`). Hooks and auto-save delegate to it. Don't re-implement.
2. **Connection pool is per-DB-path.** `connection_pool.get(str(db_path))` returns stale connections if the path doesn't exist. Active connections cannot be evicted.
3. **Vec keys/index drift after warm-up.** Run `rebuild_vec_index.py` after warm-up chains, not before.
4. **Schema migrations go in `migrations/NNN_name.sql` + `NNN_name.down.sql`.** Bump `SCHEMA_VERSION` in `migration_runner.py`. Current: **22**. Never edit live DB schema by hand.
5. **Default search is `include_global=True`** with blended RRF. Don't override "for safety."
6. **18 CORE tools are user-facing**; 71 ADMIN under `memory_maintenance(operation=...)`. Don't add CORE tools without checking.
7. **Use `--incremental` / `--full` with backfill.** Bare args create 22 MB garbage DBs at repo root.
8. **Tests hitting prod DB must use `_ProdDBGuarded` mixin.** See `eval/test_safety_wiring.py:60-109`.
9. **Lock order: file lock first, then conn.** Both `save_memory` and `_update_memory_index_incremental` follow this order.
10. **Concurrent .md writes preserve losers.** `safe_atomic_write(path, content, expected_existing=...)` saves conflicting on-disk content as `<path>.conflict-<pid>-<ts>`.
11. **CRDT merges write to .md files.** Markdown is the source of truth; stale .md after a merge is silent drift.
12. **Signal handlers installed BEFORE flock check** in `auto_save.py`. Otherwise daemon returns without handlers and ignores SIGTERM.
13. **Cross-process writes are single-writer.** Long-lived daemons hold a `flock`; cron scripts hold per-cron `flock`. Add a `flock` to any new long-lived writer.
14. **Saga rollback cleans up dependent rows.** `save.saga.undo_upsert` calls `save.cleanup.cleanup_memory_relations()` (covers kg_facts, orphan kg_edges, backlinks).
15. **Update docs after code changes.** Stale docs are a maintenance hazard — fix them in the same commit.

---

## Sync Server Security

Binds to `127.0.0.1:9877`. Key env vars: `MEMORY_SYNC_TOKEN` (required), `MEMORY_SYNC_HMAC_SECRET` (optional), `MEMORY_SYNC_TLS_CERT`/`MEMORY_SYNC_TLS_KEY` (native TLS), `MEMORY_SYNC_TLS_CLIENT_CA` (mTLS). Empty `MEMORY_SYNC_CORS_ORIGINS` means no CORS. Non-loopback without TLS logs a warning.

---

## Hook Wiring

Four lifecycle hooks (PreToolUse, SessionStart, Stop/PostToolUse, plus log helper `_log_error.py` is not a lifecycle hook). All lifecycle hooks write to STDOUT. See `~/.claude/settings.json` for wiring. `auto_save.py on_tool_complete` is wired via `opencode.jsonc`.

---

## Feature Flags

| Flag | Default | Notes |
|---|---|---|
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

## Test Execution Rules

14. **Full suite MUST use the subprocess runner.** Direct `pytest eval/` in a single process hangs or segfaults on Apple Silicon (MPS / OMP thread conflict). Always use one of:
    - **`make test`** (preferred — calls `eval/run_full_suite.py`)
    - `./venv/bin/python eval/run_full_suite.py` (direct)
    - `nohup ./venv/bin/python eval/run_full_suite.py > /tmp/full_suite.log 2>&1 &` + `tail -f /tmp/full_suite.log` (background + poll)
    - `tail -f /tmp/full_suite_runner.log` to follow progress; summary line at end: `SUMMARY: <pass>p <fail>f <skip>s <xfail>xf <xpass>xp <err>e`

    `eval/run_full_suite.py` enforces:
    - One fresh Python process per test file (no MPS/OMP state bleed)
    - `KMP_DUPLICATE_LIB_OK=TRUE` + `OMP_NUM_THREADS=1` (prevents torch + libomp conflict)
    - 600s per-file timeout
    - JUnit XML parsing for stable counts
    - Segfault detection via stderr scan

15. **Single-test invocations are fine in-process.** `pytest eval/test_foo.py::test_bar` in the agent's own shell is safe — only the *full* suite needs isolation.

16. **If a test file segfaults, it counts as 1 failure.** The runner catches `signal 11` / `Segmentation fault` / `Fatal Python error` and marks the file as `SEGFAULT` even if pytest returns 0. Check `eval/results/full_suite_results.txt` for the canonical report.

17. **Never delete `eval/test_all_*.py` files.** They re-run the whole suite as a single test file and are explicitly skipped by `run_full_suite.py` to avoid infinite recursion. Direct `pytest eval/` will still execute them.

---

## Emergency

1. **Stale lock — diagnose first.** Both `.rebuild.lock` and `.vec_rebuild.lock` use `fcntl.flock`, which auto-releases when the holding process dies. An empty lock file on disk alone is not a real contention — the actual protection is the OS-level flock held by an open FD. Before removing anything: run `ps aux | grep python` and try a non-blocking acquire yourself (the next legitimate writer will succeed automatically if no live process holds it). If a live process IS holding the lock, find it with `lsof | grep rebuild.lock` and decide whether to wait or kill it. **Never `rm` a lock that a live process is holding** — it will corrupt the write it's mid-way through. If the holder is dead (no matching PID), the empty file is safe to remove: `rm memory/.rebuild.lock`.
2. Check audit log: search for `error`, `crash`, `orphan`, `drift`.
3. Check cron logs: `memory/worker.log`, `memory/heartbeat.log`, `memory/integrity.log`.
4. Run integrity check: `venv/bin/python memory_integrity.py memory/memory.db`. 0 critical = OK.
5. Stuck? Read `eval/test_*.py` for the regression net.

---

## Database Table Gotchas

| Old / Mythical Name | Real Source |
|---|---|
| `auto_save_events` | `memory_audit_log WHERE tool='auto_save'` |
| `memory_injection_log` | `memory_audit_log WHERE error LIKE '%inject%'` |
| `crdt_operations` | `kg_entity_crdt` + `kg_edge_crdt` |
| `crdt_state` | `memory_field_crdt` |
| `arc_cache` | `arc_stats` (current) + `arc_ghosts` (evicted) |
| `memory_pinned` | `memories WHERE pinned=1` (column, not a table) |
| `tool_routing_decisions` | Removed — was never materialized |

Many of these names come from early design docs or deprecated schema
versions. If a dashboard metric shows `—` or 0, check the real source
above before creating a new table.

---


