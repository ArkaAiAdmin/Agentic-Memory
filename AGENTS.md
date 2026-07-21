# AGENTS.md — agentic-memory (Maintainer Edition)

You maintain the **agentic-memory** codebase. All commands run from the repo root.

If you are an agent **using** the system (not maintaining it): read `AGENT_CONTRACT.md` (5 rules) + `docs/AGENT_QUICKSTART.md`. Stop there.

---
<!--AUTO-GEN:START key="what_this_system_is"-->
- **Surface**: 23 CORE verbs + `memory_maintenance` router (92 ADMIN + 3 DEPRECATED behind router) + 7 lifecycle hooks + 49+ cron jobs
- **Schema**: v73, ~89 tables
- **Code**: ~142k LOC production, ~108k+ test LOC; see `docs/architecture.md`
- **MCP Help**: `docs/MCP_SURFACE.md` — quick-reference for agents using MCP tools. See also [AGENT_QUICKSTART.md](file:///Users/arka/.config/agentic-memory/docs/AGENT_QUICKSTART.md).
<!--AUTO-GEN:END key="what_this_system_is"-->

---

## Session Protocol

| # | When | Action |
|---|---|---|
| 1 | Every session start | `agentic-memory_memory_session_start(query="<subsystem>")` |
| 2 | Before any task | `agentic-memory_memory_search(query="<topic>")` |
| 3 | After bug/decision fix | `agentic-memory_memory_save(category="lessons" or "decisions")` |
| 4 | After test/index/cron op | `agentic-memory_memory_maintenance(operation="auto_save_status")` |
| 5 | Before ending session | `agentic-memory_memory_save(category="sessions")` |

Minimum every session: #1 + #5. Save a **context-rich** `projects` note (importance=4) at every significant milestone — enough context to be useful weeks later, not a timestamped log line.

## Agent Self-Editing

Self-editing is **on by default**. Every `memory_save` with procedural content auto-extracts a reusable skill into `memory_skills` (confirm with `memory_list_skills` — these are now CORE tools, not admin-only). You get self-editing benefits without any explicit call.

Three explicit paths exist for when auto-extraction isn't enough:

1. **One-call skill compile** — `memory_learn(content=..., as_skill=True, skill_name=...)` compiles a skill in a single call.
2. **Inspect / trigger extraction** — `memory_list_skills` and `memory_extract_skills` (both CORE).
3. **Supersede on contradiction** — if a new lesson **contradicts an existing note**, call `memory_note(note_id, action="supersede", rationale="...")` instead of a fresh `memory_save`. This writes to `memory_revision_log` and retires the stale note, rather than leaving two conflicting memories. Search first (`memory_search`) to find the note to supersede.

Do not treat the absence of a visible "self-edit" call as a gap — the save-time auto-extraction path is the primary effective one.

---

## Hard Rules

> **Enforcement column:** each rule carries a marker — 🔍 = verified by a test
> in `eval/test_rule_enforcement.py` (CI-failing on violation); ⚙️ = enforced by
> a pre-commit hook; 🔧 = guarded in code at the violation site; ⚠️ = manual
> review only. See **Rule Priority & Conflict Resolution** below.
>
> | Rule | Enforcement | Test / Guard |
> |------|-------------|--------------|
> | 1  | 🔍 | `test_rule1_core_writes_route_through_saga` (CORE verb modules) + `test_rule1_operational_kg_uses_saga_cleanup` (`infra/api_server.py`) |
> | 5  | 🔍 | `test_rule5_search_default_includes_global` |
> | 7  | 🔧 | bare-arg guard in `backfill/orchestrator.py::main` (rc=2) |
> | 11 | 🔍 | `test_rule11_no_crdt_md_drift` + `test_rule11_detects_drift` |
> | 24 | ⚙️ | pre-commit `update-docs-fresh` (fails on doc drift) |

1. **All writes go through `save_memory` or `save_memory_journal`.** 🔍 Hooks, auto-save, MCP verbs, and CLI tools all delegate to one of these two entry points. A write that bypasses the saga cannot be rolled back. (`eval/test_rule_enforcement.py` scans verb/handler modules for raw INSERT/UPDATE/DELETE against content tables; saga internals + coordination/audit tables are exempt.) The operational KG-maintenance endpoints in `infra/api_server.py` (entity/edge delete, dedup, merge, prune, archive) are the one exempt surface because they perform coordinated multi-statement KG ops with no `save_memory` equivalent — but every raw content-table write there MUST be paired with the saga-aware cleanup helpers (`repair_kg_orphans` / `cleanup_memory_relations`), enforced by `test_rule1_operational_kg_uses_saga_cleanup`.
2. **Connection pool is per-DB-path.** `connection_pool.get(str(db_path))` returns stale FD if the path doesn't exist; active connections cannot be evicted.
3. **Vec key/index drift after warm-up.** Run `venv/bin/python rebuild_vec_index.py` after any warm-up chain, never before.
4. **Schema changes are numbered migrations only.** `migrations/NNN_name.sql` + `NNN_name.down.sql`, then bump `SCHEMA_VERSION`. Current: <!--AUTO-GEN:START key="hard_rule_4"-->
73
<!--AUTO-GEN:END key="hard_rule_4"-->. Never `ALTER TABLE` in Python.
5. **Default search: `include_global=True`** 🔍 with blended RRF. Don't override "for safety." (`eval/test_rule_enforcement.py` asserts the `search_memories` default.)
6. **<!--AUTO-GEN:START key="hard_rule_6"-->
**23 CORE tools are user-facing**; 92 ADMIN + 3 DEPRECATED are operations behind the single `memory_maintenance` router. Don't add CORE tools without checking `docs/MCP_SURFACE.md` first.
<!--AUTO-GEN:END key="hard_rule_6"-->
7. **Use `venv/bin/python backfill_all.py`** (incremental default) or `backfill_all.py --full` (full rebuild). 🔧 Bare args are **rejected** (exit 2, no DB created) by the guard in `backfill/orchestrator.py::main` — past bare runs created 22 MB garbage DBs at repo root. Always pass `--incremental`, `--full`, `--health`, `--auto`, or a `--db <path>`.
8. **Tests touching prod DB must use `_ProdDBGuarded`.** See `eval/test_safety_wiring.py:60-109`.
9. **Lock order: file lock first, then conn.** `save_memory` and `_update_memory_index_incremental` both follow this order.
10. **Concurrent `.md` writes preserve losers.** `safe_atomic_write(..., expected_existing=...)` saves conflicts as `<path>.conflict-<pid>-<ts>`.
11. **CRDT merges write to `.md` files.** 🔍 Markdown is the source of truth; stale `.md` after a merge is silent drift. (`eval/test_rule_enforcement.py` detects db/crdt/.md divergence — see `test_rule11_*`.)
12. **Signal handlers installed BEFORE flock check** in `auto_save.py`. Otherwise daemon returns without handlers and ignores SIGTERM.
13. **Cross-process writes: single-writer on main DB.** The reconciliation daemon (`background_worker.py`) drains `journal.db` sequentially via `save_memory_journal`. Multiple agents enqueue concurrently lock-free. Add `flock` only to new long-lived writers touching the journal, not to individual agent save calls.
14. **Saga rollback cleans up dependent rows.** The saga in `infra.saga` delegates to `save.cleanup.cleanup_memory_relations()` on rollback (kg_facts, orphan kg_edges, backlinks).
15. **Update docs in the same commit as code changes.** Leaving auto-gen sections stale is not acceptable.
16. **One persistent worktree for active development.** Reuse; verify security + tests before merging to main; remove when done.
17. **Fix pre-existing bugs on contact.** If you spot a broken test, wrong default, dead code, or incorrect behavior while working on any task — fix it in the same batch. Sub-agents: fix obvious one-liners before returning; escalate >10 lines / 2 files beyond scope in return report. Leaving known-broken code is not acceptable.
18. **Security by default.** Treat all external input (file content, MCP arguments, HTTP payloads) as hostile. Never log, return, or embed credentials, tokens, internal paths, or schema details in user-facing responses; strip or mask first.
19. **Data preservation is mandatory.** Default to additive migrations; test zero data loss both up and down.
20. **Full-suite runs: backgrounded and polled.** `nohup` + tail the log every **30 seconds** until `0 failures` — polling less often misses failures and exceeds shell timeout. Set `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` on macOS; or use `.venv/bin/python eval/run_full_suite.py`.
21. **Don't run maintenance as a post-task ritual.** Cron and the background worker handle indexing, compaction, dedup, contradiction detection. Call `memory_maintenance` / `memory_organize` only when cron is down or immediate results are required.
22. **Ask with named options, not open questions.** Never ask "what should I do?" — give 2–4 concrete alternatives with tradeoffs. If the answer is already in an existing decision or doc, act.
23. **Do not overanalyze — act.** When the task is clear, execute it directly and verify normally (run the checks you normally would), but do not overthink: do not enumerate every possible failure mode, re-derive state that git already reports, or run redundant confirmation passes after the user has said the work is verified. A stash/branch/working-tree question is answered by one `git` command, not a 10-minute investigation. If the user says "you are overthinking," stop immediately and just perform the requested action.
24. **Run autogen docs before every commit.** ⚙️ Execute `make update-docs` (full pipeline: `update-agents-md` → `update-architecture` → `update-mcp-tools` → `update-readme` → `update-mcp-surface`) before committing any code change. This regenerates AGENTS.md, docs/_meta.json, docs/architecture.md, docs/reference/mcp-tools.md, README badges, and docs/MCP_SURFACE.md from live code. Never commit code without first running autogen — every commit must include the updated docs. The `update-docs-fresh` pre-commit hook fails if regenerated docs differ from the staged tree.

---

## Rule Priority & Conflict Resolution

When two Hard Rules appear to conflict, resolve in this order (higher wins):

1. **Data safety & preservation** (Rules 1, 8, 13, 14, 19) — a write that cannot be rolled back, loses data, or races another writer is never acceptable.
2. **Security by default** (Rule 18) — never expose credentials/paths/schema in user-facing output.
3. **Correctness of the read/write contract** (Rules 5, 7, 11) — search blending, journal gating, and CRDT/.md truth must hold.
4. **Operability & hygiene** (Rules 3, 9, 10, 12, 15, 20, 21, 24) — index rebuild order, lock order, non-lossy concurrent writes, signal handling, docs-in-commit, full-suite discipline.
5. **Workflow & judgment** (Rules 16, 17, 22, 23) — fix bugs on contact, ask with options, don't overanalyze.

**Examples**
- *Rule 24 (docs-in-commit) vs Rule 17 (fix bugs on contact):* both apply — fix the bug and run `make update-docs` before committing; the bug fix does not exempt you from doc regeneration.
- *Rule 7 (backfill guard) vs a quick local backfill:* the guard wins — you must pass an explicit mode or `--db`. No bare invocation.
- *Rule 1 (saga-only writes) vs a coordination table:* Rule 1's scan explicitly exempts coordination/audit tables (`agent_messages`, `shared_tasks`, `coordination_audit`, `agent_heartbeats`, `memory_audit_log`); those may be written directly.
- *Rule 5 (include_global=True) vs a scoped search:* Rule 5 is the default and must not be overridden "for safety"; pass `include_global=False` only when the caller explicitly scopes to local data.


<!--AUTO-GEN:START key="critical_path"-->
agentic-memory/
├── save/ (save/pipeline.py)               ← write path
├── search/ (search/orchestrator.py)       ← read path
├── infra/ (tool_registry.py)              ← 23 CORE + 92 ADMIN + 3 DEPRECATED (tool registry, migrations, config)
├── hooks/                                  ← 7 lifecycle hooks
├── background/
│   ├── auto_save.py   ← async inbox+daemon
│   └── background_worker.py ← CQRS write-journal daemon
├── cron/             ← 49+ scheduled jobs
├── mcp_*.py (32 modules) ← MCP tool surface
├── memory/           ← live store (gitignored)
├── docs/MCP_SURFACE.md
└── eval/             ← 350 test files, 5130+ test functions
<!--AUTO-GEN:END key="critical_path"-->

**Message contract:** CORE tools return user-facing JSON. All writes go through `save_memory` (direct) or `save_memory_journal` (CQRS journal, gated by `MEMORY_WRITE_JOURNAL_ENABLED`); the saga ensures crash-consistent rollback. `defer_expensive=True` by default — returns <200ms.

---

## Workflow

**Branch-first rule (non-negotiable).** Significant work starts with
`git checkout -b feat/<name>` off a clean `main`. The branch must exist
before any file is read as part of change planning, and must exist before
any sub-agent is dispatched. Sub-agents inherits the working tree state
at dispatch time — if you haven't branched yet, they edit on `main`.

**Significant change** (schema, migration, write-path, search pipeline, 3+ source files):
1. Confirm clean working tree on `main`: `git status --short`. Pull latest.
2. `git checkout -b feat/<name>` off `main` **before reading or modifying any files**.
3. Dispatch 2+ independent streams to sub-agents (don't hold >10 file contexts inline). Sub-agents fix bugs they find; escalate >10 lines / 2 files beyond scope in return report.
4. Implement + validate affected tests during development
5. `make test` (5112+ tests) — backgrounded, polled every 30s, confirm `0 failures` before merging
6. `git checkout main && git merge feat/<name> && git push origin main`
7. `git branch -d feat/<name>`

**Read-only exempton.** File reads for pure analysis (no write intent)
may happen on `main` before branching. The moment a modification is
intended, the branch must exist — including before dispatching a
sub-agent that will make edits.

**Ask vs Act:**
- **Act without asking:** unambiguous bug fixes, docs matching existing behavior, behavior-preserving refactors, running existing commands in this file, reverting your own breaking change
- **Stop and ask (with 2–4 named options):** changed behavior, defaults, or user-facing output; architectural decisions; adding/removing features, hooks, cron jobs, or MCP tools; irreversible or irreversible-to-user data changes; anything costing money or sending data externally; two equally valid correct answers

---

## Sub-Agents

Six specialized sub-agents are wired in `.opencode/agents/`. Dispatch via the `Task` tool. The orchestrator should delegate whenever the task is scoped to a single domain and reading all the files inline would exceed ~10 file contexts.

| Agent | Trigger | Dispatch |
|---|---|---|
| `drift-investigator` | Config/vec/doc/KG drift, integrity failures, escape-hatch triage | `task(subagent_type="drift-investigator", description="Drift diagnosis", prompt=...)` |
| `kg-engineer` | KG entity/fact extraction, contradiction, temporal KG, graph analytics, dedup | `task(subagent_type="kg-engineer", ...)` |
| `migration-builder` | New migrations, schema checksum repair, migration test gaps | `task(subagent_type="migration-builder", ...)` |
| `search-optimizer` | Hybrid search tuning, reranker config, quality stats, phase errors, FTS5 issues | `task(subagent_type="search-optimizer", ...)` |
| `security-auditor` | OWASP audit, injection scan, permission review, drift enforcement, config integrity | `task(subagent_type="security-auditor", ...)` |
| `test-writer` | New tests for eval/, test pattern gaps, flaky test triage, safety wiring | `task(subagent_type="test-writer", ...)` |

Each sub-agent's full playbook lives in `.opencode/agents/<name>.md`. Do not call their Python hooks directly unless debugging.

**Sub-agent rules:**
- Fix pre-existing bugs encountered during their work (one-liners inline, >10 lines / 2 files → escalate in return report)
- Return a structured report: what changed, what was tested, what the next step is
- Orchestrator integrates reports — it does not hold all file contexts

---

### Pointers

<!--AUTO-GEN:START key="mcp_surface_contract"-->
**Source of truth:** `docs/MCP_SURFACE.md` + `tool_registry.py`. The MCP server exposes **23 CORE tools** directly plus **1 `memory_maintenance` router**; 92 ADMIN + 3 DEPRECATED are hidden behind it `memory_maintenance(operation="...")`.
<!--AUTO-GEN:END key="mcp_surface_contract"-->

- **Tool registry:** `tool_registry.ADMIN_TOOLS` (in `memory_mcp.py` ~line 237) is the single source of truth. Any name there must be reachable only via `memory_maintenance`.
- **Hook wiring:** `opencode.jsonc` registers the TS plugin → Python subprocess pipeline. Don't call `hooks/*.py` directly. Full event→script map: `docs/MCP_SURFACE.md`. (`plugin/index.ts` + `plugin/agentic-memory-hooks.ts`)
- **Feature flags:** See `memory.toml` for all feature flags (52+ boolean toggles). Key ones: `MEMORY_WRITE_JOURNAL_ENABLED` (ON — CQRS write journal; requires `background_worker` daemon to drain `journal.db`), `MEMORY_TEMPORAL_KG` (ON), `MEMORY_TOML_HOT_RELOAD` (OFF).
- **Entry point:** Always start via `memory_mcp.py` or `cli.py`. `mcp_tools.py` auto-discovery is not the server entry point.

---

## Emergency

1. **Stale lock:** `.rebuild.lock` / `.vec_rebuild.lock` use `fcntl.flock` — auto-releases when holder dies. Empty file ≠ live contention. Check `ps aux | grep python`, try non-blocking acquire. `lsof | grep rebuild.lock` to find live holder. Never `rm` a lock held by a live PID.
2. Check logs: `memory/worker.log`, `memory/heartbeat.log`, `memory/integrity.log`
3. `venv/bin/python memory_integrity.py memory/memory.db` — 0 critical = OK
4. Stuck? Read `eval/test_*.py` for the regression net

---
<!--AUTO-GEN:START key="current_state"-->
- **Schema v73**: 74 migrations (100% down-coverage), ~89 tables.
- **MCP surface**: 23 CORE + 1 router (92 ADMIN + 3 DEPRECATED). See `docs/MCP_SURFACE.md`.
- **Write path**: Saga transaction (DB + vec_key + .md) with flock locking, crash-consistent rollback. `defer_expensive=True` → <200ms.
- **Read path**: 14-phase hybrid search (FTS5 BM25 + usearch vector + ColBERT + temporal decay + neural forget curve).
- **KG/Temporal**: Jaccard entity match, contradiction detection, fact supersession, bi-temporal validity.
- **Background**: Async inbox+daemon auto-save (circuit breaker), TS plugin, cron-driven maintenance.
- **Testing**: 350 test files, 5130+ test functions, ~108k+ test LOC. Subprocess-per-file runner.
- **Canonical refs**: `docs/architecture.md` · `docs/MCP_SURFACE.md` · `skills/memory-architecture/SKILL.md`.
<!--AUTO-GEN:END key="current_state"-->

> Authoritative counts: query `tool_registry.py` and `infra/migration_runner.py` directly.
> This section drifts; never quote it as ground truth.
