# AGENTS.md — Agentic Memory System (Maintainer Edition)

You are an agent working on the **agentic-memory** codebase at the repo root.

**If you are an agent using this system** (not maintaining it): read `AGENT_CONTRACT.md` — 5 rules for every session.

**If you are maintaining this codebase**: everything below this line is for you. The Reliability Rules table has the quick-reference; the Hard Rules have the detail.

---

## What This System Is

Local-first, MCP-server-shaped memory layer for AI agents. All data at `~/.config/agentic-memory/memory/`.

<!--AUTO-GEN:START key="what_this_system_is"-->
- **Surface**: 17 CORE verbs + `memory_maintenance` router (87 ADMIN + 3 DEPRECATED behind router) + 8 lifecycle hooks + 46+ cron jobs
- **Schema**: v37, ~49 tables
- **Code**: ~103k LOC production, ~87k+ test LOC; see `docs/architecture.md`
- **MCP Help**: `docs/MCP_SURFACE.md` — quick-reference for agents using MCP tools. See also [AGENT_QUICKSTART.md](file:///Users/arka/.config/agentic-memory/docs/AGENT_QUICKSTART.md).
<!--AUTO-GEN:END key="what_this_system_is"-->

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
| 9 | .md/DB drift | `venv/bin/python memory_integrity.py <db> --recover-orphan-files` |
| 10 | KG/backlinks orphans | `venv/bin/python memory_integrity.py <db> --repair-kg-orphans` |
| 11 | Auto-save history | `agentic-memory_memory_maintenance(operation="circuit_breaker_status")` |
| 12 | Temporal KG misbehaving | Set `MEMORY_TEMPORAL_KG=0` |
| 13 | Every significant milestone or decision | `agentic-memory_memory_save` a **context-rich periodic note** — captures goal, approach, rationale, improvements, semantic relationships. Not a timestamped log line: it should carry enough context to be useful weeks later. Category: `projects`. Tags: include decision/subsystem context. Importance: 4. |

Minimum: do #1, #7, and #13. Run #8 opportunistically. Use `agentic-memory_memory_maintenance(operation="compliance_check")` to audit.

**Reference:** `docs/MCP_SURFACE.md` has the full verb reference, decision tree, and parameter tables.

## Critical Path

<!--AUTO-GEN:START key="critical_path"-->
agentic-memory/
├── save/ (save/pipeline.py)          ← write path (saga, FTS5, chunks, embeddings, KG, facts, audit, CRDT)
├── search/ (search/orchestrator.py)  ← read path (FTS5 BM25 + usearch vector + ColBERT + temporal decay + neural forget curve)
├── infra/ (tool_registry.py)         ← 17 CORE + 87 ADMIN + 3 DEPRECATED (single source of truth; tool_registry.py + memory_mcp.py + mcp_maintenance.py)
├── hooks/                            ← 8 lifecycle hook implementations + 1 log helper
├── background/
│   ├── auto_save.py                  ← async inbox+daemon entry point
│   ├── inbox.py                      ← inbox management + daemon lifecycle
│   ├── daemon.py                     ← long-lived inbox drainer
│   ├── background_worker.py           ← CQRS write-journal reconciler daemon
│   ├── tool_complete.py              ← hook → save_memory pipeline
│   └── circuit_breaker.py            ← auto-save failure gating
├── cron/                             ← 46+ scheduled jobs + install_crontab.sh
├── mcp_*.py (30 modules)             ← domain-split MCP tools
├── memory/                           ← live store (gitignored)
├── docs/MCP_SURFACE.md               ← MCP tool reference for agents
└── eval/                             ← 271 test files, 4346+ test functions
<!--AUTO-GEN:END key="critical_path"-->

**Message contract:** All CORE tool responses are user-facing JSON. Admin tools (87 ADMIN + 3 DEPRECATED) are routed exclusively through `memory_maintenance(operation="...")` — never call an ADMIN tool name directly. All writes go through `save_memory` (direct) or `save_memory_journal` (CQRS journal, gated by `MEMORY_WRITE_JOURNAL_ENABLED`); the saga ensures crash-consistent rollback with dependent-row cleanup. `defer_expensive=True` by default — returns <200ms.

---

## Hard Rules

1. **All writes go through `save_memory` or `save_memory_journal`.** Hooks, auto-save, and MCP verbs delegate to one of these two entry points. The `save_memory_journal` path (gated by `MEMORY_WRITE_JOURNAL_ENABLED`) enqueues writes to the CQRS journal for async materialization by the reconciliation daemon. A write that bypasses both is a write that can't be rolled back.
2. **Connection pool is per-DB-path.** `connection_pool.get(str(db_path))` returns stale connections if the path doesn't exist. Active connections cannot be evicted.
3. **Vec keys/index drift after warm-up.** Run `venv/bin/python rebuild_vec_index.py` after warm-up chains, not before.
4. **Schema migrations go in `migrations/NNN_name.sql` + `NNN_name.down.sql`.** Bump `SCHEMA_VERSION` in `migration_runner.py`. Current: **<!--AUTO-GEN:START key="hard_rule_4"-->
37
<!--AUTO-GEN:END key="hard_rule_4"-->**. Never edit live DB schema by hand.
5. **Default search is `include_global=True`** with blended RRF. Don't override "for safety."
6. <!--AUTO-GEN:START key="hard_rule_6"-->
**17 CORE tools are user-facing**; 87 ADMIN + 3 DEPRECATED are operations behind the single `memory_maintenance` router. Don't add CORE tools without checking `docs/MCP_SURFACE.md` first.
<!--AUTO-GEN:END key="hard_rule_6"-->
7. **Use `venv/bin/python backfill_all.py` (incremental default) or `venv/bin/python backfill_all.py --full` (full rebuild).** Bare args create 22 MB garbage DBs at repo root.
8. **Tests hitting prod DB must use `_ProdDBGuarded` mixin.** See `eval/test_safety_wiring.py:60-109`.
9. **Lock order: file lock first, then conn.** Both `save_memory` and `_update_memory_index_incremental` follow this order.
10. **Concurrent .md writes preserve losers.** `safe_atomic_write(path, content, expected_existing=...)` saves conflicting on-disk content as `<path>.conflict-<pid>-<ts>`.
11. **CRDT merges write to .md files.** Markdown is the source of truth; stale .md after a merge is silent drift.
12. **Signal handlers installed BEFORE flock check** in `auto_save.py`. Otherwise daemon returns without handlers and ignores SIGTERM.
13. **Cross-process writes are single-writer on the main DB.** The reconciliation daemon (`background_worker.py`) is the single writer to the main memory DB — it drains the CQRS write journal (`journal.db`) sequentially. Multiple agent processes enqueue concurrently via `save_memory_journal` (lock-free `INSERT` with WAL). Add a `flock` only to new long-lived writers that touch the journal or rebuild indexes, not to individual agent save calls.
14. **Saga rollback cleans up dependent rows.** `save.saga.undo_upsert` calls `save.cleanup.cleanup_memory_relations()` (covers kg_facts, orphan kg_edges, backlinks).
15. **Update docs after code changes.** Stale docs are a maintenance hazard — fix them in the same commit.
16. **Use one persistent worktree for active development.** Reuse it for all ongoing feature work; do not create a new worktree per branch or per commit. Verify security and tests in the worktree before merging to main. Keep worktrees minimal and remove them when no longer needed.
17. **Fix every LSP error in every file you touch.** Every file you read or edit must exit with zero LSP errors (pyright). No `# type: ignore` comments, no `# noqa` for type errors, no silent `except` swallowing of type-correctness issues. Fix the type annotation at the source (function signature, variable declaration) so the error is resolved correctly. Pre-existing errors in files you didn't modify are exempt, but any file you edit must be left fully clean.
18. **Run mypy + ruff before every commit.** Before committing any changes, run `venv/bin/python -m mypy <any file you modified>` and `venv/bin/python -m ruff check <any file you modified>`. Fix all errors and warnings. Do not commit with outstanding mypy or ruff issues. This applies even to test files.
19. **Maintenance is automated.** Most maintenance is handled by cron jobs and the background worker (see `cron/install_crontab.sh`). The agent should exercise `memory_organize`, `memory_maintenance`, or individual MCP tools **only when cron is not running or immediate results are needed**. Do not run maintenance tools as a default post-task ritual.
 20. **Full-suite test runs must be backgrounded and monitored.** Any `pytest eval/` or full-suite invocation must be run with `nohup` in the background, with the log file polled **every 30 seconds** (see Hard Rule 24) until completion. Do not run the full suite as a blocking foreground call — it will exceed the shell timeout and you will miss failures. Always tail the log file and confirm `0 failures` before declaring the suite green. **Important macOS constraint**: When executing tests in-process using `pytest`, you must set `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` (e.g., `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES .venv/bin/pytest eval/ -q`) to prevent the macOS Objective-C runtime fork checks from triggering a segmentation fault when other daemon threads (write-queue, revalidator, etc.) are active. Alternatively, run the subprocess-isolated test suite: `.venv/bin/python eval/run_full_suite.py`.
 21. **Use sub-agents to conserve the orchestrator's context window.** When a task can be decomposed into two or more independent work streams (e.g. fixing multiple unrelated test files, investigating separate subsystems, running parallel verifications), dispatch each stream to a sub-agent via the `Task` tool instead of reading all the files inline. Return from each sub-agent a structured report of what it changed and what the next step is. The orchestrator's job is coordination and integration, not context accumulation. Sub-agents are mandatory whenever the parallel work would otherwise require the orchestrator to hold more than ~10 files of source/test context simultaneously. **Sub-agents must fix every pre-existing bug they encounter during their work.** A bug is "obvious" if it is a one-line fix: wrong variable name, broken import, type error, dead code block, wrong default value, missing null check, or any behavior that is clearly not what the code intends. If the bug is small (≤ ~10 lines to fix, or a single-line change), the sub-agent must fix it before returning. If the bug requires more than ~10 lines or touches more than 2 files beyond the sub-agent's own changes, the sub-agent must document it precisely in its return report — file, line, what is wrong, what the fix should be — so the orchestrator can dispatch a follow-up. Sub-agents must never ignore pre-existing bugs, mark them "out of scope," or leave them for "someone else." They may complete their own deliverable first and then fix the bugs, but the bugs must be fixed or explicitly escalated in the same dispatch cycle.
 22. **Pre-existing bugs and test failures must be fixed, not ignored.** If you discover a pre-existing bug, failing test, or broken behavior while working on any task, you must fix it, add or update tests to cover it, verify the fix with the affected test(s) or full suite, and report the fix back to the user. Leaving known-broken code or known-failing tests behind is not acceptable.
 23. **Update AGENTS.md when critical agent-facing information changes.** If you change any of the following, run `make update-agents-md` in the same commit to regenerate AUTO-GEN sections: schema version, table counts, MCP tool surface counts (CORE/ADMIN/DEPRECATED totals), channel/key names in search weights, hook wiring, CLI commands, Critical Path diagram, or any information agents rely on to call tools correctly. Stale AGENTS.md is as harmful as stale code.
 24. **Poll the test log every 30 seconds during any full-suite run.** When running the full test suite (`make test` / `pytest eval/`), launch it with `nohup` in the background and read the log file (e.g. `tail -5 /tmp/<suite>.log`) **exactly every 30 seconds** until it finishes. Do not batch longer waits, do not rely on the process exiting to discover failures, and do not declare the suite green until the final summary line reports `0 failures`. If you see `F` markers or a non-zero failure count at any poll, keep monitoring to the end, then triage every failure before merging or pushing.

---

## Constitution

These principles govern every decision in this codebase. They override convenience.

1. **Schema changes must be reversible.** Every `.sql` migration must have a matching `.down.sql`. A change that can't be rolled back is not a migration — it's a data loss incident waiting to happen.

2. **All schema changes go through numbered migrations.** Never `ALTER TABLE` or `CREATE TABLE` directly in Python code unless the table is ephemeral (cache/temp). If a Python setup function needs a persistent table, create it as a numbered migration so the down-up round-trip is provably correct.

3. **All writes go through `save_memory`.** Hooks, auto-save, CLI tools — all delegate to `save_pipeline.save_memory`. A write that bypasses the saga is a write that can't be rolled back.

4. **Write idempotent SQL.** Every `CREATE` must use `IF NOT EXISTS`. Every `DROP` must use `IF EXISTS`. Idempotency is the difference between a safe retry and a silent corruption.

5. **Every architectural decision goes into memory.** If it's not saved as a `decisions` or `lessons` note, it didn't happen. The note must answer: what was the problem, what were the options, why was this one chosen, and what are the tradeoffs?

6. **Test both the happy path and the failure path.** If a migration silently skips a statement (expected table missing, duplicate column), there must be a test that proves the final schema is identical regardless of the order operations ran.

7. **When in doubt, ask with named options.** Never ask "what should I do?" Give 2-4 concrete alternatives with tradeoffs. The user's time is valuable — don't waste it on open-ended questions.

8. **Security by default.** This system handles private human memories. Treat all external input — file content, MCP arguments, HTTP payloads — as hostile. Never log, return, or embed credentials, tokens, internal paths, or schema details in user-facing responses; strip or mask before surfacing externally.

9. **Data preservation is mandatory.** Every schema or code change must migrate existing data automatically. Default to additive migrations; a change that requires manual data repair, causes silent data loss, or breaks reads of older rows is not acceptable. Migration tests must assert zero data loss both up and down.

10. **Fix pre-existing bugs on contact.** If you discover a pre-existing bug, failing test, broken behavior, or any incorrect code while working on any task — regardless of whether it's in your immediate task scope — you must fix it in the same batch, add or update tests to cover it, verify the fix, and report it back. Leaving known-broken code or known-failing tests behind is not acceptable. A bug fix does not require user approval when the correct behavior is unambiguous.

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

## Significant Feature Workflow

When the task is a significant feature, refactor, or any change that affects schema, migrations, core write/read paths, or spans multiple files:

| # | Step | Action |
|---|---|---|
| 1 | Branch | `git checkout -b feat/my-feature` off `main` before writing code |
| 2 | Sub-agents | For tasks decomposable into 2+ independent work streams (multiple test files, separate subsystems, parallel verification), dispatch each stream to a sub-agent via the `Task` tool. The orchestrator coordinates and integrates reports — it does not hold all file contexts inline. |
| 3 | Build | Implement the change on the feature branch (or in sub-agent reports) |
| 4 | Validate | Run individual test files for affected areas during development |
| 5 | Full suite | Before merging, run `make test` (in-process, all 4,000+ tests) and confirm 0 failures |
| 6 | Merge local | `git checkout main && git merge feat/my-feature` |
| 7 | Push | `git push origin main` |

**Rules:**
- Never commit significant work directly to `main`. Always use a feature branch.
- "Significant" means schema changes, migration additions/repairs, write-path modifications, search pipeline changes, MCP tool surface changes, or anything that touches 3+ source files.
- The full `make test` suite must pass with 0 failures before merging to local `main`.
- Small unambiguous fixes (typos, single-file bugs, test-only changes) may go directly to `main` without branching.

---

## Sync Server Security

Binds to `127.0.0.1:9877`. Key env vars: `MEMORY_SYNC_TOKEN` (required), `MEMORY_SYNC_HMAC_SECRET` (optional), `MEMORY_SYNC_TLS_CERT`/`MEMORY_SYNC_TLS_KEY` (native TLS), `MEMORY_SYNC_TLS_CLIENT_CA` (mTLS). Empty `MEMORY_SYNC_CORS_ORIGINS` means no CORS. Non-loopback without TLS logs a warning.

---

## MCP Surface Contract

<!--AUTO-GEN:START key="mcp_surface_contract"-->
**Source of truth for the MCP tool surface: `docs/MCP_SURFACE.md` + `tool_registry.py`**. The MCP
server exposes **17 CORE tools** directly plus **1 `memory_maintenance` router**; 87 ADMIN + 3 DEPRECATED are hidden behind it
`memory_maintenance(operation="...")`.

| Tier | Count | Access |
|------|-------|--------|
| CORE verbs | 17 | Direct MCP tool call |
| ADMIN (legacy) | 87 | `memory_maintenance(operation="...")` or `memory_advanced(operation="...")` |
| DEPRECATED | 3 | Same as ADMIN (also listed in ADMIN_TOOLS; tracked for audit) |
<!--AUTO-GEN:END key="mcp_surface_contract"-->

**When to use which (decision tree):**

## 1. Session Start (Hard Rule #1 — always first)

| Trigger | Tool | Key Params | Defaults |
|---------|------|-----------|----------|
| Every new session or task switch | memory_session_start | query: subsystem/task name | empty = general briefing |

## 2. Save

| Trigger | Tool | Key Params | Defaults |
|---------|------|-----------|----------|
| Lesson learned, bug fixed, insight | memory_learn | content, category, tags, importance | category="lessons", importance=3, pinned=False |
| Explicit memory (event, decision, preference, session) | memory_save | content, category, tags, importance, pinned | category="lessons", importance=3, pinned=False |
| Edit existing note | memory_note | note_id, action, content, rationale | action="update" for full replace |
| Insert/delete fragments in a note | memory_note | note_id, action="patch", additions=[], deletions=[], rationale | additions and deletions are lists of strings |
| Retire a note and create a replacement | memory_note | note_id, action="supersede", title_slug, rationale | rationale required |
| Undo a supersession | memory_note | note_id, action="revert_supersede", rationale | rationale required |
| Review auto-saved drafts | memory_curate_autosave | action, note_ids | action="list" (no note_ids) |
| Promote a draft to intentional memory | memory_curate_autosave | action="promote", note_ids=[...] | — |
| Discard an unwanted draft | memory_curate_autosave | action="discard", note_ids=[...] | — |

## 3. Search / Find

| Trigger | Tool | Key Params | Defaults / Notes |
|---------|------|-----------|-----------------|
| Default lookup (all modes) | memory_search | query, mode, limit | mode="hybrid", limit=10, include_global=True |
| Semantic/vector search only | memory_search | query, mode="semantic" | use when full-text noise is high |
| Full-text search only | memory_search | query, mode="fts" | fast, no embedding required |
| KG facts only | memory_search | query, mode="facts" | combine with belief_status, epistemic_source, fact_type |
| Knowledge graph traversal | memory_search | query, mode="graph" | — |
| Filter facts by belief | memory_search | mode="facts", belief_status | active \| retracted \| deprecated \| unconfirmed |
| Filter facts by source | memory_search | mode="facts", epistemic_source | agent \| auto_save \| hook \| import \| cron |
| Filter facts by type | memory_search | mode="facts", fact_type | observation \| agent_inference \| external_stated \| hypothesis \| derived |
| Filter by how memory was created | memory_search | memory_source | agent \| auto_save \| import |
| Bounded recall (recent context + recall log) | memory_recall | query, session_id | empty query = recent activity |
| Explore knowledge graph | memory_graph | query, action | action="explore" \| "traverse" \| "shortest_path" \| "stats" |
| Walk from a specific entity | memory_graph | action="traverse", start=entity_id, max_depth | max_depth=2 |
| Find path between two entities | memory_graph | action="shortest_path", start, end | — |
| Graph statistics | memory_graph | action="stats" | — |

## 4. Review / Curate / Clean Up

| Trigger | Tool | Key Params | Defaults |
|---------|------|-----------|----------|
| Review low-confidence or stale beliefs | memory_review_beliefs | min_confidence, older_than_days, limit | min_confidence=0.5, older_than_days=30, limit=20 |
| Safe maintenance batch | memory_organize | target, dry_run | target="safe_default", dry_run=False |
| Full maintenance batch | memory_organize | target="full", dry_run=False | full = safe_default + backfill + dedup + purge_expired |
| Compact FTS5 index | memory_organize | target="compact", dry_run | — |
| Deduplicate KG entities | memory_organize | target="dedup", dry_run | — |
| Delete a note | memory_delete | note_id, hard | hard=False (soft-delete, 30-day recovery) |
| Check system health | memory_health_check | — | — |

## 5. Share (multi-agent)

| Trigger | Tool | Key Params | Defaults |
|---------|------|-----------|----------|
| Share a note with another agent | memory_share | note_id, share_with | — |
| View notes shared with current agent | memory_share | note_id="", action="list" | — |

## 6. Admin / Diagnostic (behind memory_maintenance router)

| Trigger | Tool | Key Params | Notes |
|---------|------|-----------|-------|
| Any admin/diagnostic op | memory_maintenance(operation="<name>", **kwargs) | operation, kwargs | 87 ops — never call ADMIN tools by name [Hard Rule #6] |
| Examples | memory_maintenance | operation="tier_stats" | |
| Examples | memory_maintenance | operation="check_integrity", deep=True | |
| Examples | memory_maintenance | operation="audit", hours=24 | |
| Review recent activity / errors | memory_audit | hours, limit, include_errors | hours=24, limit=20, include_errors=True |
| View user/agent/skills profile or ARC stats | memory_profile | action="stats\|user\|agents\|skills\|arc" | action="stats" for overview |
| Power user escape hatch | memory_advanced(operation="<any admin op>") | operation, kwargs | Identical to memory_maintenance |

---

**Pre-flight (every session):**
1. `memory_session_start(query="<current subsystem>")` — loads context from previous sessions [Hard Rule #1]
2. Search before acting: `memory_search(query="<topic>")` before any task [Hard Rule #2]
3. Never call ADMIN tools by name — go through `memory_maintenance` [Hard Rule #6]

**After significant work:**
- Save decisions/lessons with `memory_save(category="decisions"/"lessons")` [Hard Rules 3, 13]
- Save session summary before ending [Hard Rule 7]
- Do NOT call `memory_organize` or `memory_maintenance` as a routine post-task ritual — cron handles this [Hard Rule 19]

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
|---|---|
| `MEMORY_WRITE_JOURNAL_ENABLED` | OFF | CQRS write journal (lock-free multi-writer). Requires background_worker daemon. When enabled, `save_memory_journal` is used instead of `save_memory`; writes are enqueued to journal and materialized asynchronously. |
| `MEMORY_TEMPORAL_KG` | ON | Event-time extraction, contradiction detection, supersession. Set `0` to disable if false contradictions or edit invalidation are too aggressive. `kg_facts.locked = 1` prevents per-fact supersession. |
| `MEMORY_TOML_HOT_RELOAD` | OFF | Live-reload of the drift policy + `[drift_tiers]` when `memory.toml` changes, no restart. Opt-in and OFF by default (auto-reloading enforcement policy in prod is surprising). Starts a background poller daemon; every reload is audited to `memory/config_drift_audit.jsonl` (`decision=toml_hot_reload`). Manual operator CLIs: `hooks/memory_toml_reload.py`, `hooks/memory_tier_patch.py`. |

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

## Current State

<!--AUTO-GEN:START key="current_state"-->
- **Schema v37**: 38 migrations (100% down-migration coverage), ~49 tables.
- **MCP surface**: 17 CORE verbs + 1 `memory_maintenance` router (87 ADMIN + 3 DEPRECATED). Agents see 18 tools. See `docs/MCP_SURFACE.md` for verb reference.
- **Write path**: Saga transaction (DB + vec_key + .md file) with flock-based cross-process locking, crash-consistent rollback, and dependent-row cleanup. `defer_expensive=True` by default — returns <200ms.
- **Read path**: 12-phase hybrid search (FTS5 BM25 + usearch vector + ColBERT + cross-encoder + temporal decay + neural forget curve + concept/centrality boost). Phase-level error counters.
- **KG/Temporal**: Entity extraction with Jaccard fuzzy match, temporal KG with contradiction detection and fact supersession, bi-temporal validity.
- **Background**: Async inbox+daemon auto-save with circuit breaker, TS plugin coordination, cron-driven maintenance.
- **Testing**: 271 test files, 4346+ test functions, ~87k+ test LOC. Subprocess-per-file runner for torch-safe parallelism.
- **Canonical references**: `docs/architecture.md` (architecture), `docs/MCP_SURFACE.md` (MCP workflow), `docs/reference/mcp-tools.md` (tool catalog), `skills/memory-architecture/SKILL.md` (agent walkthrough).

> Note: For authoritative counts, query `tool_registry.py` and `infra/migration_runner.py` directly.

> Note: Current Status is a point-in-time snapshot. It will drift. For authoritative counts, query the codebase directly.
<!--AUTO-GEN:END key="current_state"-->
