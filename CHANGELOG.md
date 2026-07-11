# Changelog

All notable changes to agentic-memory are documented here. The format
is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [2.2.0] — 2026-07-12 — Enterprise Readiness

### Features

- **SSO/JWT authentication** — Phase 2 scaffold: `authlib_sso.py`, `mcp_auth.py`,
  migration 047, tool registry wiring. API server `_require_auth` JWT rewrite
  with `memory.toml` SSO config.
- **Audit sink** — Phase 3 scaffold: `audit_sink.py` with file/http/prom sinks,
  `audit.py` wiring, `memory.toml` config. 3 test files covering HTTP, 5xx,
  and principal redaction scenarios.
- **GDPR erasure** — Phase 4: ADMIN tool, REST endpoint, tests, docs.
  Migration 049, `gdpr.py` module.
- **Tenant isolation** — Phase 0: harden cross-tenant isolation. Phase 1:
  RBAC foundation + tenant-isolation test fixes. `tenant_id` column on
  `memories`, `kg_entities`, `kg_facts`, `memory_field_crdt` tables.
- **KG belief-temporal hardening** — Belief layer, temporal queries,
  contradiction merge wiring. Inception-fingerprint identity for entity
  dedup. `UNIQUE(fingerprint)` replaces `UNIQUE(name, entity_type)`.
- **SSO sync metadata** — `memory_sso_sync_metadata`, `memory_sso_idp_list`,
  `memory_sso_idp_add` tools. ACL ops, config bug fix, tenant migrations.
- **Enterprise-grade sync** — Tenant filter, doc drift enforcement,
  REST e2e isolation test.

### Fixes

- **Audit principal/tenant capture** — Capture caller principal+tenant in
  all audit rows. Deny empty-string `principal_id` unconditionally in
  `mcp_authorize`.
- **Fail-closed + tenant isolation gaps** — Close remaining fail-closed
  gaps (xfail count 4→1).
- **SSO XXE** — Raises `SsoAuthError` via `defusedxml`. Migration 050 down
  drops indexes before column.
- **Test fixes** — Remove `toml` dep, drop `tenant_id` fallback, fix 050
  down migration. Restore correct indentation on `enqueue_audit` call.
- **KG entity FK bug** — Migration 019: `kg_facts.subject_entity_id` and
  `kg_facts.object_entity_id` now have `ON DELETE SET NULL`.
- **Save pipeline deadlock** — Break deadlock cycle by lazy-loading
  `save.indexers` and `save.post_save_hooks`.
- **Worker memory leak** — Disable LLM extractor in drain/once modes
  to prevent 4-6 GB memory leak.
- **Search results content** — Search results now include `content` field
  (was always `""`).

### Security

- **SEC-1 CRITICAL: sync_server.py auth bypass** — `_require_auth()` returns
  403 on non-loopback interfaces when `MEMORY_SYNC_TOKEN` is unset.
- **SEC-4 HIGH: HMAC dead-code removal** — Unreachable `return True` after
  `hmac.compare_digest` removed.
- **CORS `*` default fixed** — Empty `SYNC_CORS_ORIGINS` defaults to no
  CORS header (was `*`).
- **Plaintext HTTP warning** — Loud warning at startup when bound to
  non-loopback without TLS.
- **PII regex expansion** — Now covers `auth_token`, `auth_header`,
  `bearer`, `authorization`, `service_token`, `integration_secret`,
  `webhook_secret`.
- **SSO XXE protection** — Uses `defusedxml` for XML parsing.
- **JWT exp hardening** — Migration 048, missing tests added.
- **MPS kernel hang guard** — `search/rerankers.py` checks
  `reranker_disabled` before loading neural cross-encoder.

### Breaking

- **Schema v56** — 57 migrations (was v37, 38 migrations). `tenant_id`
  column on `memories`, `kg_entities`, `kg_facts`, `memory_field_crdt`.
  `principal_identities` table (migration 043). `tenant_memories` TEMP VIEW.
- **Schema v50** — `kg_entities` and `kg_facts` gain `tenant_id` column
  (migration 050) for cross-tenant KG data protection.
- **Schema v53** — `memory_field_crdt` gains `tenant_id` column
  (migrations 051, 053).
- **Tool registry** — 95 ADMIN + 3 DEPRECATED tools (was 87 ADMIN).
  New SSO, ACL, GDPR, and tenant tools added.
- **Authlib migration** — Migrated from `authlib.jose` to `joserfc`.

### Docs

- **AGENTS.md** — Hard Rule 23 (do not overanalyze — act). Sub-Agents
  dispatch section. Lean rewrite 375→129 lines.
- **MCP_SURFACE.md** — Tenant isolation + RBAC documentation. Belief
  layer status update.
- **Architecture** — Phase 0/1 tenant isolation + RBAC in `architecture.md`.
- **Security doc** — Audit sink security documentation.
- **Benchmark docs** — Real performance benchmarks published. Numbers
  flagged as measured under Lightroom memory pressure.

## [2.1.0] — 2026-06-26 — Ecosystem Integration Layer

### Features

- **LangChain integration** — `AgenticMemoryRetriever`, `AgenticMemoryChatHistory`,
  `search_tool` + `save_tool`, `AgenticMemoryCallbackHandler`.
- **CrewAI integration** — `AgenticMemorySearchTool` + `AgenticMemorySaveTool`,
  `AgenticMemoryMemory` crew memory slot adapter.
- **OKF export/import** — `memory_okf_export`, `memory_okf_import` tools
  for Open Knowledge Format interop.
- **Shared memory pool** — `memory_auto_share`, `memory_shared_import`,
  `memory_shared_list`, `memory_shared_stats` tools.
- **Federated skills** — `memory_list_federated_skills`,
  `memory_resolve_contradiction` tools.
- **Background task status** — `memory_background_task_status` tool.

### Fixes

- **Save pipeline connection leak** — `save_memory` now calls
  `safe_close_db(conn)` on the saga path via `try/finally`.
- **Lock-order inversion** — Fixed deadlock: file lock → conn in both
  saga and incremental paths.
- **Active-conn eviction** — `_evict_lru` skips active connections and
  raises `PoolExhaustedError` if every conn is active.
- **Unbounded inbox queue** — `AUTO_SAVE_INBOX_MAX_BYTES` (default 100 MB)
  prevents disk-fill DoS.
- **Lock bypass** — `_acquire_lock` re-raises `FileLockError` instead of
  returning None.
- **Embedding model upgrade** — Skip check requires BOTH `content_hash`
  AND `model_revision` to match.
- **Inbox drain race** — Rename-and-process pattern prevents data loss.
- **Signal handler ghost-daemon** — Install signal handlers before
  flock check.
- **Concurrent global writes** — `safe_atomic_write` with
  `expected_existing` snapshot preserves conflict files.
- **Schema migration mid-flush** — `save_memory` invalidates pragma cache
  before any schema-feature read.
- **Orphan files** — `find_orphan_files` + `recover_orphan_files` in
  `memory_integrity.py`.
- **Cron installer lock** — POSIX-portable `mkdir`-based lock.
- **FTS5 drift auto-healing** — `repair_fts_drift` in `memory_integrity.py`.
- **CRDT merge to .md files** — `_write_merged_markdown` /
  `_finalize_crdt_save` helpers.
- **Saga + recalc atomicity** — `_recalculate_fitness_scores` takes
  `conn` parameter for atomic success_score update.

### Security

- **SEC-1: CORS `*` default fixed** — Empty allowlist = no CORS header.
- **SEC-3: import_shared_memory half-indexed** — Uses
  `should_commit=False` on failure to roll back.
- **SEC-4: Plaintext HTTP warning** — Loud warning when bound to
  non-loopback without TLS.

## [2.0.0] — 2026-06-22 — Production-Ship Quality

### Features

- **Async/background-batch auto-save** — JSONL inbox with daemon.
  Per-call latency dropped ~95% (100-200ms → 2-5ms).
- **God-function decomposition** — `save_memory` 216→110 lines (-49%).
  `_run_post_save_hooks` 113→40 lines (-65%). `search_memories`
  551→244 lines (-56%).
- **Rate limiting** — `infra/rate_limiter.py` with per-tool RPM and
  burst support. `memory.toml [rate_limits]` configuration.
- **Feature flag visibility** — `log_feature_flags_at_startup()` emits
  JSON snapshot of all 17 feature flags.
- **Cron robustness** — Flock-based model-load mutex with stale lock
  detection. Dual locking for embedding/kg backfill.
- **Observability** — Search phase timing, `memory_stats` admin op.
- **CI integration** — GitHub Actions workflow with ruff, mypy, pytest.
  Pre-commit hooks for tool-registry drift and schema version.
- **KG dashboard** — Streamlit KG graph view with force-directed layout.
- **Benchmark scripts** — `eval/benchmarks/bench_save.py`,
  `eval/benchmarks/bench_search.py`.
- **SDK as pip-installable** — `agentic_memory/` package with
  `MemoryClient`, `AgentMemory`. 6-subcommand CLI.
- **Migration 016** — `concept_drift` table moved to canonical SQL.
- **Near-dup dedup** — O(N log N) sort + sliding-window Jaccard.

### Fixes

- **P0-1 through P0-5** — Connection leak, lock-order inversion,
  active-conn eviction, unbounded inbox, lock bypass.
- **P1-1, P1-2** — Embedding model upgrade skip, inbox drain race.
- **Signal handler ghost-daemon** — Install handlers before flock.
- **Scenario 4, 5, 7, 10, 11** — Concurrent writes, schema migration,
  orphan files, cron installer lock, FTS5 drift.
- **Cron health check lock order** — `acquire_lock_or_exit` moved
  to start of `main()`.
- **Test circuit-breaker state leak** — `setUp()` calls
  `_auto_save_reset_state()`.
- **make_lazy_getattr cache bug** — Caches in target module's `__dict__`.
- **saga.__exit__ return type** — Was `bool`, now `Literal[False]`.
- **contradiction_detector fallback** — `safe_close_db` signature matches.
- **pyproject.toml coverage gate** — Was 0 (disabled), now 60.
- **141 mypy errors** — Reduced to 0 across 28 files.

### Breaking

- **Schema v16** — `concept_drift` table canonical SQL migration.
- **Cron cadence** — Reduced from `*/5` to `*/15`.
- **Cron scripts** — Moved from repo root to `cron/cron_*.py`.
- **God-module refactor** — `save_pipeline.py` (1,709→948 LOC),
  `search_pipeline.py` (3,532→1,834 LOC), `backfill_all.py`
  (1,721→761 LOC).

## [1.1.0] — 2026-06-26 — Search Pipeline Content Fix

### Fixes

- **Search results content field** — `search/orchestrator.py`
  `_build_result_items()` now includes `content` in result dict.

## [1.0.0] — 2026-06-26 — Ecosystem Integration Layer

### Features

- **LangChain adapters** — `AgenticMemoryRetriever`,
  `AgenticMemoryChatHistory`, `search_tool`, `save_tool`,
  `AgenticMemoryCallbackHandler`.
- **CrewAI adapters** — `AgenticMemorySearchTool`,
  `AgenticMemorySaveTool`, `AgenticMemoryMemory`.

### Docs

- **Integration docs** — `docs/integrations/overview.md`,
  `langchain.md`, `crewai.md`, `roadmap.md`.
- **Examples** — `examples/langchain_agent.py`,
  `examples/crewai_crew.py`.

## [0.x] — Pre-release — Saga Atomicity + Monolith Split

### Features

- **Saga transactions** — Crash-consistent writes with undo/redo.
- **Per-field CRDT** — Field-level LWWES for concurrent edits.
- **12-phase search pipeline** — BM25 + vector + ColBERT + RRF +
  cross-encoder + temporal decay + neural forget + KG boost.
- **Knowledge graph** — Entity extraction, temporal edges, contradiction
  detection, graph analytics.
- **Adaptive retention** — Neural forget curve with surprise-based
  retention formula.
- **Tier system** — Hot/warm/cold/archive memory lifecycle.
- **Sync server** — Native TLS, mTLS support.
- **Monolith split** — `memory_mcp.py` (~5,000 LOC) split into 17
  `mcp_*.py` domain modules.

### Fixes

- **Saga + recalc atomicity** — `_recalculate_fitness_scores` takes
  `conn` parameter.
- **adaptive_retention cache** — Module-level cache bypasses O(N×M)
  audit log scan.
- **make_lazy_getattr cache** — Caches in target module's `__dict__`.
- **141 mypy errors** — Reduced to 0.

### Security

- **Sync server auth** — `_require_auth()` returns 403 on non-loopback
  when token unset.
- **HMAC dead-code removal** — Unreachable `return True` removed.
- **PII regex expansion** — Covers additional token types.
