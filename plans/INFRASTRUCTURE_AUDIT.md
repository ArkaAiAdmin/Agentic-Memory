# Infrastructure Audit: Background, Sync, Coordination & MCP

**Date:** 2026-07-23 | **Scope:** background/, cron/, sync layer, coordination/, mcp_*.py

---

## 1. Background Automation (background/)

### 1.1 Subsystem Map

| Module | Purpose | Status |
|---|---|---|
| `daemon.py` | Auto-save inbox daemon — kqueue/inotify/sleep-poll loop, flock-protected, batch processing | **Healthy** |
| `auto_save.py` | Hook entry (tool-complete, capture-draft, daily-digest), dedup, circuit breaker | **Healthy** |
| `background_queue.py` | SQLite-backed task queue, `enqueue_task()` API | **Healthy** |
| `background_worker.py` | Task consumer — handler registry, WorkerPool, vec drift reconciliation | **Healthy** |
| `inbox.py` | File-based inbox with pending-file at-least-once semantics | **Healthy** |
| `circuit_breaker.py` | Failure tracking for daemon, shared memory state | **Healthy** |
| `config.py` | Tool allowlist/denylist, batch config, TOML+env resolution | **Healthy** |
| `fleet_entry.py` | Multi-process reconciler fleet supervisor | **Healthy** |
| `fleet_worker.py` | Single shard worker for fleet | **Healthy** |
| `corpus_budget_guard.py` | Compaction trigger when corpus exceeds budget | **Healthy** |
| `daily_digest.py` | Renders daily auto-save summary markdown | **Healthy** |
| `tool_complete.py` | Per-tool auto-save capture logic | **Healthy** |
| `cron_model_lock.py` | File-based mutex for ML model-loading crons | **Healthy** |
| `adaptive_retention.py` | Metadata half-life updates for decay | **Healthy** |
| `retention_coordinator.py` | Unifies adaptive_retention + neural_forget pipeline | **Healthy** |
| `purge.py` | Bulk soft-delete of auto-save entries | **See Gap G4** |

### 1.2 Findings

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| G1 | LOW | `purge.py:36` opens a raw `sqlite3.connect()` instead of using `connection_pool`. Bypasses tenant isolation and pool management. | `background/purge.py:36-38` |
| G2 | LOW | `retention_coordinator.py` runs adaptive retention and neural forget sequentially. Could parallelize with ThreadPoolExecutor for ~2× speedup on large corpora. | `background/retention_coordinator.py:27-45` |

---

## 2. Cron Scheduler (cron/)

### 2.1 Scheduler Architecture

Single `cron/scheduler.py` runs every 5 minutes via crontab. Reads `cron/jobs.py` (the registry), determines which jobs are due, runs them as subprocesses. Uses flock for process-singleton. 49 jobs registered across 5 frequency tiers (5m, 15m, 30m, 1h, 6h, 1d, 1w, 1m).

### 2.2 Orphaned Cron Files (NOT registered in scheduler)

**4 cron scripts exist on disk but are NOT in `cron/jobs.py`:**

| File | Purpose | Has Tests? | Impact |
|---|---|---|---|
| `cron_answer_rerank.py` | Tunes answer-reranking weights from CTR feedback | No direct tests | CTR learning never runs automatically |
| `cron_recompute_temporal_priors.py` | Fits temporal decay half-life priors | `eval/test_temporal_half_life.py` | Temporal priors never update automatically |
| `cron_review_beliefs.py` | Reviews/asserts belief lifecycle state | `eval/test_cron_review_beliefs.py` | Belief lifecycle never runs automatically |
| `cron_tune_rewrites.py` | Tunes rewrite quality weights via logistic regression | `eval/test_ctr_weight_learning.py` | Rewrite quality weights never update |

**Severity: MEDIUM.** These scripts have lock files, logging, and test coverage — they were clearly intended to run. They can only execute manually. This means:
- CTR-based weight learning is dead (answer_rerank + tune_rewrites)
- Temporal priors are static (never adapt to actual usage patterns)
- Belief lifecycle assertions are never checked

### 2.3 `cron_runs.py` — Not an orphan

`cron_runs.py` is a utility module (recording/querying cron execution history). It's imported by `scheduler.py` and is not a standalone job. Correctly excluded from the registry.

---

## 3. Sync Layer

### 3.1 Architecture

All top-level sync modules (`sync_server.py`, `sync_client.py`, `sync_check.py`, `sync_invariant.py`) are backward-compat shims delegating to `infra/sync_*.py`.

**Real implementations:**

| Module | Lines | Purpose |
|---|---|---|
| `infra/sync_server.py` | 1764 | Threaded HTTP server — `/health`, `/crdt/changes`, `/crdt/push` endpoints. Bearer token auth, HMAC signing, CORS allowlist, replay protection. |
| `infra/sync_client.py` | 718 | Pull/push CRDT changes from peer agents. HTTP client with retry. |
| `infra/sync_check.py` | 66 | Invariant checks for sync consistency. |
| `infra/sync_invariant.py` | 292 | Deep invariant verification (version vectors, content hashes). |
| `infra/sync_server_daemon.py` | 85 | Standalone entry point for the sync server. Launched by `cron_crdt_sync.py`. |

### 3.2 Findings

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| G3 | LOW | Sync server binds `127.0.0.1:9877` over plain HTTP. No TLS. Acceptable for localhost-only, but CRDT payloads (memory content) traverse unencrypted. A local process with `nc` access can intercept. | `infra/sync_server.py:41` — `HTTPServer` (stdlib, no SSL wrapping by default) |
| G4 | LOW | `sync_server_daemon.py:76` — `signal.pause()` blocks indefinitely on platforms that support it. If the signal handler fires between the `while` check and `pause()`, the daemon misses the stop signal and stays alive until the next signal. Minor — SIGTERM is reliable on macOS/Linux. | `infra/sync_server_daemon.py:75-76` |

**What's working well:**
- Bearer token auth on mutating endpoints (B8 fix)
- HMAC payload signing (Y2 fix)
- Replay protection via timestamp (Y3 fix)
- CORS allowlist (Y1 fix) — never defaults to `*`
- mDNS discovery for peer agents

---

## 4. Agent Coordination (coordination/)

### 4.1 Architecture

Five submodules with clean separation:

| Module | Purpose | Key API |
|---|---|---|
| `locking.py` | File locks with fencing tokens (monotonic version) | `acquire_lock_fenced()`, `verify_lock_fenced()`, `FencingLock` |
| `messaging.py` | Inter-agent message queue + dead-letter | `send_message()`, `broadcast_message()`, `process_dead_letters()` |
| `project_state.py` | Per-project key-value state | `get_state()`, `set_state()`, `get_active_files()` |
| `durability.py` | Heartbeats, audit, crash recovery | `update_heartbeat()`, `run_durability_maintenance()`, `cleanup_stale_agents()` |
| `hooks.py` | Integration with save/search/cron pipelines | `acquire_save_lock()`, `create_and_dispatch_task()` |

All properly exported via `coordination/__init__.py` (65 lines, complete `__all__`).

### 4.2 Findings

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| G5 | LOW | `mcp_coordination.py` (468 lines) is a substantial MCP surface for coordination tools. It's registered in `tool_registry.py` as `memory_coordinate` (CORE). Verify the MCP handlers properly delegate to `coordination/` primitives and don't duplicate logic. | `mcp_coordination.py:468 lines` |
| G6 | INFO | Coordination uses SQLite tables (`file_locks`, `agent_messages`, `project_state`, `agent_heartbeats`, `coordination_audit`) — all exempt from Rule 1 (saga-only writes) per AGENTS.md. This is correct and documented. | AGENTS.md Rule 1 exemption |

**What's working well:**
- Fencing tokens prevent TOCTOU races
- Dead-letter queue for undeliverable messages
- Fail-safe hooks (all wrapped in try/except, return safe defaults)
- Separate connections for coordination (don't interfere with main pool)

---

## 5. MCP Server Surface

### 5.1 Architecture

- **Entry point:** `mcp_tools.py` — auto-discovery via glob for `mcp_*.py`, two-phase import (registration + re-export)
- **Tool registry:** `tool_registry.py` — 24 CORE + 92 ADMIN + 3 DEPRECATED
- **Router:** `mcp_maintenance.py` (1360 lines) — single `memory_maintenance(operation="...")` router for all ADMIN tools
- **Instance:** `mcp_instance.py` (8 lines) — MCP server instance singleton

### 5.2 MCP Module Coverage

| Module | Tools | Tier | Status |
|---|---|---|---|
| `mcp_memory.py` (782L) | Core save/search/delete/recall | CORE | **Healthy** |
| `mcp_search.py` (398L) | Search variants | CORE | **Healthy** |
| `mcp_verbs.py` (1242L) | Verb routing (note, learn, audit, organize) | CORE | **Healthy** |
| `mcp_maintenance.py` (1360L) | ADMIN router | ADMIN | **Healthy** |
| `mcp_maintenance_ops.py` (1733L) | ADMIN operation implementations | ADMIN | **Healthy** |
| `mcp_kg.py` (518L) | KG tools | CORE/ADMIN | **Healthy** |
| `mcp_coordination.py` (468L) | Coordination tools | CORE | **Healthy** |
| `mcp_audit.py` (829L) | Audit tools | ADMIN | **Healthy** |
| `mcp_auth.py` (367L) | Auth/SSO/RBAC | ADMIN | **Healthy** |
| `mcp_health.py` (332L) | Health checks | CORE | **Healthy** |
| `mcp_crdt.py` (133L) | CRDT sync/status | ADMIN | **Healthy** |
| `mcp_ctr_drift.py` (242L) | CTR drift tracking | ADMIN | **Healthy** |
| `mcp_rebuild.py` (235L) | Index rebuild | ADMIN | **Healthy** |
| `mcp_session.py` (165L) | Session management | CORE | **Healthy** |
| `mcp_sharing.py` (152L) | Cross-agent sharing | ADMIN | **Healthy** |
| `mcp_maintenance_policy_hash.py` (167L) | Policy hash verification | ADMIN | **Healthy** |
| `mcp_okf.py` (146L) | OKF import/export | ADMIN | **Healthy** |
| `mcp_common.py` (138L) | Bootstrap, shared helpers | — | **Healthy** |
| `mcp_async.py` (132L) | Async MCP wrappers | — | **Healthy** |
| `mcp_kg_traversal.py` (115L) | KG traversal tools | ADMIN | **Healthy** |
| `mcp_metrics.py` (103L) | Metrics export | ADMIN | **Healthy** |
| `mcp_agent.py` (92L) | Agent context tools | CORE | **Healthy** |
| `mcp_sdk.py` (92L) | SDK demo tools | ADMIN | **Healthy** |
| `mcp_quality.py` (79L) | Quality gate tools | ADMIN | **Healthy** |
| `mcp_profile.py` (67L) | User profile tools | CORE | **Healthy** |
| `mcp_summarization.py` (67L) | Summarization tools | ADMIN | **Healthy** |
| `mcp_retention.py` (57L) | Retention pipeline | ADMIN | **Healthy** |
| `mcp_safety.py` (120L) | Safety/injection scanning | ADMIN | **Healthy** |
| `mcp_multi_modal.py` (48L) | Multi-modal ingestion | ADMIN | **Healthy** |
| `mcp_dashboard.py` (162L) | Dashboard tools | ADMIN | **Healthy** |
| `mcp_instance.py` (8L) | Server singleton | — | **Healthy** |
| `infra/mcp_singleton.py` (207L) | Singleton enforcement | — | **Healthy** |

### 5.3 Findings

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| G7 | LOW | `mcp_multi_modal.py` is only 48 lines — likely a thin stub. Verify it has real implementations, not just `pass` or `raise NotImplementedError`. | `mcp_multi_modal.py:48 lines` |
| G8 | INFO | `memory_incremental_update` appears in both ADMIN_TOOLS and DEPRECATED lists. This is intentional (deprecated tools are still listed in ADMIN for backward compat routing) but could confuse auditors. | `tool_registry.py:96,142` |

---

## 6. Summary of Gaps

### Actionable (register the orphans)

| Priority | Gap | Fix |
|---|---|---|
| **MEDIUM** | 4 orphaned cron scripts never run automatically | Register `cron_answer_rerank`, `cron_recompute_temporal_priors`, `cron_review_beliefs`, `cron_tune_rewrites` in `cron/jobs.py` at appropriate frequency tiers |
| LOW | `purge.py` bypasses connection pool | Use `connection_pool.get()` instead of raw `sqlite3.connect()` |
| LOW | Sync server has no TLS | Document that TLS termination is the operator's responsibility; or add optional TLS wrapping |
| LOW | `retention_coordinator.py` runs sequentially | Parallelize with ThreadPoolExecutor (minor optimization) |

### Informational (no action needed)

| Gap | Note |
|---|---|
| `cron_runs.py` not in registry | Correct — it's a utility module, not a job |
| `memory_incremental_update` in both ADMIN and DEPRECATED | Intentional backward compat |
| Coordination tables exempt from saga | Documented in AGENTS.md Rule 1 |
| Fleet entry points are standalone scripts | Correct — designed for subprocess spawning |

---

## 7. What's Solid

The infrastructure is well-architected:

1. **Background daemon** — signal-before-flock (Rule 12), at-least-once pending files, kqueue/inotify/sleep fallback, circuit breaker, idle exit
2. **Cron scheduler** — single entry point, flock-protected, 49 jobs across 5 tiers, subprocess isolation, run recording
3. **Sync layer** — bearer token + HMAC + replay protection + CORS, CRDT merge on push, mDNS discovery
4. **Coordination** — fencing tokens, dead-letter queues, fail-safe hooks, clean module separation
5. **MCP surface** — auto-discovery, clean tier separation (CORE/ADMIN/DEPRECATED), single router for ADMIN, 24 CORE tools well-distributed across 31 modules

The biggest concrete gap is the **4 orphaned cron scripts** — they represent dead automation that should be running but isn't.
