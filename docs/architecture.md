# Architecture

Agentic Memory is a local-first, markdown-primary persistent memory system for AI agents.

## Core Principles

1. **Markdown is source of truth for user content** — the body and
   frontmatter of each `.md` note is the authoritative copy.  The SQLite
   database is *mostly* derivable from markdown (FTS5, embeddings,
   chunks) but some relational metadata exists ONLY in SQLite:
   CRDT version vectors, KG edges, access logs, concept drift metrics,
   ARC state, the task queue, the sync log, and the shared memory pool.
   A full rebuild (`backfill_all.py --full`) logs a warning listing
   these unrecoverable tables.
2. **One-directional data flow** — markdown → index, never reversed
3. **No LLM in the write path** — deterministic extraction only
4. **Graceful degradation** — works without any process running
5. **Local-first** — all data stays on your machine

## Data Flow

```
User/Agent
    │
    ▼
┌─────────────┐
│ Save Pipeline │
│  (save_*)    │
└──────┬──────┘
       │
       ├──▶ Markdown files (source of truth)
       ├──▶ SQLite FTS5 index (full-text search)
       ├──▶ Knowledge graph (entities + relations)
       ├──▶ Vector embeddings (optional)
       └──▶ Background tasks (async processing)
```

## Search Pipeline

```
Query
  │
  ├──▶ FTS5 BM25 (keyword match)
  ├──▶ Vector search (semantic match, optional)
  ├──▶ Knowledge graph (entity lookup)
  │
  ▼
┌──────────────┐
│ Ranker        │
│ (hybrid score)│
└──────┬───────┘
       │
       ▼
   Results (top-k)
```

## Save Pipeline

The canonical write path (`_update_memory_index_incremental` +
`_run_post_save_hooks`) runs the following 13 steps in order:

1. Lock acquire
2. Compute tier + PRAGMA setup
3. Upsert memory row (DB + tier inline)
4. CRDT version bump
5. Index backlinks (wiki-style)
6. Index chunks
7. Index embedding
8. Index KG (entities + edges)
9. Index facts (SPO triples)
10. Auto semantic backlinks (FTS overlap)
11. Auto FTS backlinks
12. Adaptive retention index
13. Enrich context + commit + post-hooks (fitness recalc + background tasks)

## Module Map

### Package Structure

```
agentic-memory/                    # Repo root — 102 production modules, 42,373 root-level LOC
├── agentic_memory/                # Python package (pip installable; 2 files)
│   ├── __init__.py                 # Re-exports Memory, AgentMemory, main
│   └── __main__.py                 # python -m agentic_memory
├── cli.py                          # 11 CLI entry points
├── memory_mcp.py                   # MCP server (thin orchestrator)
├── save_pipeline.py                # Write path (~1,623 LOC, re-export shim → save/)
├── save/                           # Write path subpackage (2026-06-20)
│   ├── __init__.py                 # Public API, re-exports
│   ├── crdt_helpers.py             # CRDT snapshot extraction
│   ├── indexers.py                 # FTS/embedding/chunk index writes
│   ├── backlinks.py                # Auto-backlink computation
│   └── post_save_hooks.py          # Fitness recalc, tier update, audit (decomposed 2026-06-22)
├── search_pipeline.py              # Read path (re-export shim → search/)
├── search/                         # Read path subpackage (2026-06-20)
│   ├── __init__.py                 # Public API, re-exports
│   ├── query_parser.py             # Query type detection, expansion, FTS
│   ├── rerankers.py                # Cross-encoder, late interaction
│   ├── scoring.py                  # RRF fusion, temporal decay, CTR
│   ├── synthesis.py                # BB1/BB2 synthesis, multi-turn
│   ├── chunk_index.py              # Chunk search, Graph-RAG expansion
│   ├── instrumentation.py          # Timing/log/observability
│   └── orchestrator.py             # 1,811 LOC — search_memories + 28 helpers (decomposed 2026-06-22)
├── backfill_all.py                 # Audit pipeline (re-export shim → backfill/)
├── backfill/                       # Audit pipeline subpackage (2026-06-20)
│   ├── __init__.py                 # Public API, re-exports
│   ├── index_backfills.py          # FTS, embedding, chunk, backlink, vec
│   └── kg_backfills.py             # KG facts, KG graph, entity filter
├── auto_save.py                    # Tool-call auto-save + async daemon (2,469 LOC)
├── knowledge_graph.py              # Entity extraction
├── fact_extraction.py              # SPO triple extraction
├── kg_dedup.py                     # Exact + semantic dedup
├── contradiction_detector.py       # Conflict detection
├── background_queue.py             # SQLite-backed task queue
├── background_worker.py            # Task queue worker (flock-protected, 120s timeout)
├── embedding_search.py             # Semantic search via model2vec
├── memory_injection.py             # Prompt injection detection
├── memory_common.py                # Shared utilities
├── db.py                           # Connection pool with re-entrancy guard
├── migration_runner.py             # Schema migrations (current v21, 21 migrations)
├── ... (102 modules total)
```

| Module | Layer | Purpose |
|--------|-------|---------|
| `save_pipeline.py` + `save/` | Write | Orchestrates markdown → index writes (~1,623 LOC shim + 5 submodules, ~1,400 LOC; 24+11=35 functions) |
| `search_pipeline.py` + `search/` | Read | BM25 + vector + KG hybrid search (shim + 8 submodules, ~4,500 LOC; `search/orchestrator.py` 1,811 LOC with 28 functions) |
| `backfill_all.py` + `backfill/` | Maintenance | Audit pipeline for index rebuilds |
| `auto_save.py` | Hook | Tool-call auto-save + async/background-batch daemon (2,469 LOC, 44 functions) |
| `knowledge_graph.py` | Write | Pattern-based NER, entity storage |
| `fact_extraction.py` | Write | Regex-based SPO triple extraction |
| `kg_dedup.py` | Maintenance | Exact + semantic entity deduplication |
| `contradiction_detector.py` | Quality | Detect conflicting facts |
| `background_queue.py` | Infra | SQLite-backed async task queue |
| `background_worker.py` | Infra | Task queue worker |
| `embedding_search.py` | Search | model2vec semantic search |
| `memory_injection.py` | Safety | Prompt injection detection |
| `tier_migration.py` | Lifecycle | Hot/warm/cold tier management |
| `spaced_repetition.py` | Review | SM-2 scheduling |
| `cross_session_learn.py` | Learning | Pattern extraction from sessions |

### Subpackage organization (2026-06-20)

The three god modules (`save_pipeline.py`, `search_pipeline.py`,
`backfill_all.py`) were decomposed into subpackages in the
2026-06-20 refactor. The original files now contain only re-export
shims — all logic lives in the submodules.

**`save/`** (write path, ~1,400 LOC total):
- `crdt_helpers.py` — extract CRDT snapshots for field-level sync
- `indexers.py` — FTS5, embedding, chunk index writes
- `backlinks.py` — auto-backlink computation and graph update
- `post_save_hooks.py` — fitness recalc, tier migration,
  memory_field_crdt sync, audit flush. Decomposed 2026-06-22 from
  a single 113-line `_run_post_save_hooks` into 7 named hook
  helpers + a 40-line orchestrator.

**`search/`** (read path, ~4,500 LOC total):
- `query_parser.py` — query type detection, expansion, FTS search
- `rerankers.py` — cross-encoder scoring, late interaction
- `scoring.py` — RRF fusion, temporal decay, neural forget curve,
  CTR channel weights (`_CTR_WEIGHTS_CACHE` lives here)
- `synthesis.py` — BB1 sentence synthesis, BB2 multi-turn history
  (`_BB2_TURNS` lives here)
- `chunk_index.py` — chunk-based search, Graph-RAG expansion
- `instrumentation.py` — timing, logging, observability
- `orchestrator.py` (1,811 LOC) — `search_memories` has 16 Phase
  comments in `search/orchestrator.py`: parse → skill-first → cache
  → FTS5 → KG facts → embedding fallback → hybrid fusion → temporal
  filter → chunk enhance → rerank → output build → safety demoting
  → quality gates → user profile boost → record access (CTR +
  cache); decomposed 2026-06-22 from a 551-line god-function into
  11 named helpers + a 244-line orchestrator (56% reduction).

**`backfill/`** (audit pipeline):
- `index_backfills.py` — FTS, embedding, chunk, backlink, vec
  index, CRDT vector, tier backfills
- `kg_backfills.py` — KG facts extraction, KG graph derivation,
  entity stopword filter (`_ENTITY_STOPWORDS` lives here)

### Write-path helpers (2026-06-22)

- `save/saga.py` — The `Saga` class supports a
  `lock_already_held` kwarg (callers that already hold the
  file lock can re-enter) and an `initial_file_content`
  parameter for conflict-aware rollback detection.  When a
  step fails mid-transaction, the saga compares the on-disk
  `.md` against `initial_file_content`; if they differ the
  conflict is recorded and the loser's version is saved as
  `<path>.conflict-<pid>-<ts>` instead of being silently
  overwritten.
- `save/indexers.py` — Pragma cache is **invalidated** on
  `journal_mode` / `synchronous` / `foreign_keys` change
  instead of just reset.  P0 cache poisons are no longer
  sticky.
- `save/post_save_hooks.py` — Decomposed from a 113-line
  `_run_post_save_hooks` god-function into 7 named helpers
  (fitness recalc, tier migration, field_crdt sync, audit
  flush, backlink update, FTS5 sync, embedding refresh) plus
  a 40-line orchestrator.
- `memory_common.py::safe_atomic_write(path, content,
  expected_existing=...)` — used by `saga.py` and the
  CRDT writers to write `.md` files atomically.  If the
  on-disk file differs from `expected_existing`, the
  previous version is preserved as a `.conflict-<pid>-<ts>`
  sibling before the new content is committed.

**Backward compatibility**: All existing imports
(`from save_pipeline import X`, `from search_pipeline import Y`,
`from backfill_all import Z`) continue to work. The `_ProxyModule`
subclass on `search_pipeline` forwards `_CTR_WEIGHTS_CACHE` writes
to `search.scoring` so test patterns that reset via direct
assignment still work.
## Database Schema

Schema version **21** (defined in `migration_runner.py`). 21
migrations applied, ~51 tables total (~31 user-visible: 28 domain + 3 FTS virtual). Recent deltas:
- v13: `memory_field_crdt` table for per-field LWWES
- v14: `arc_ghosts` + `arc_stats` tables for ARC eviction
- v15: `drift_alarms` table + `memory_embeddings.ssm_state` column
- v16: `concept_drift` table moved to canonical SQL migration
  (was previously created in Python via `db_migrations._migrate_concept_drift`,
  which violated AGENTS.md hard rule 7 — every other table has a
  numbered `.sql` file). The Python helper stays as a safety net
  (`CREATE TABLE IF NOT EXISTS`) for un-migrated DBs.
- v17: `kg_edges.kg_entities` and `backlinks.memories` FK constraints
  added (B-3 fix 2026-06-22 follow-up). `kg_edges` uses
  `ON DELETE SET NULL` (entities are shared across notes, so
  CASCADE would be too eager). `backlinks` uses `ON DELETE CASCADE`
  so deleting a memory cleans up its outgoing links.
  `kg_entities` is left without a FK (shared); orphans are cleaned
  by `memory_integrity.repair_kg_orphans`.
- v18: fact-level temporal KG (T1 of the temporal-kg plan). Adds 9
  columns to `kg_facts` for bi-temporal validity (event_time,
  event_time_granularity, transaction_time, valid_at, invalid_at,
  superseded_by, supersedes, contradiction_score,
  invalidation_reason) + 3 indexes (validity, superseded_by,
  event_time). See [Temporal KG concept doc](concepts/temporal-kg.md).
- v19: kg_facts entity FKs (`subject_entity_id`, `object_entity_id`)
  get `ON DELETE SET NULL`. Fixes a pre-existing bug where
  `kg_dedup.merge_entities()` would fail with `FOREIGN KEY
  constraint failed` when a fact referenced the merged entity.
- v20: `kg_facts_fts` FTS5 virtual table + 3 sync triggers (ai, ad,
  au). Brings kg_facts in line with the other 3 text-searchable
  tables (`memories`, `memory_chunks`, `kg_entities`) which all
  had FTS5. Enables ranked search via `MATCH` (O(log n) vs O(n)
  for the previous `LIKE %query%` query in `facts_search()`).
- v21: `kg_entity_crdt` + `kg_edge_crdt` tables for CRDT-based
  multi-agent KG merge support (2P-Set entity ops, LWW edge ops).

### Core Tables

- **memories** — Memory metadata (id, category, title, tags, tier, timestamps)
- **chunks** — Searchable text chunks with positional data
- **memory_embeddings** — Vector embeddings for semantic search (now with `ssm_state` v15)
- **task_queue** — Background task queue (pending/processing/completed/failed)
- **kg_entities** — Extracted entities (name, type, mention count)
- **kg_edges** — Entity relationships (source_id, target_id, relation, weight)
- **kg_facts** — Extracted SPO triples with confidence scores (v18 temporal cols, v19 entity FKs with ON DELETE SET NULL, v20 FTS5, v21 kg_crdt)
- **memory_audit_log** — Audit trail for observability
- **memory_field_crdt** (v13) — per-field LWWES CRDT state
- **arc_ghosts** (v14) — Adaptive Replacement Cache ghost lists
- **arc_stats** (v14) — Adaptive Replacement Cache stats key/value
- **drift_alarms** (v15) — per-memory concept-drift alarms with severity tiers
- **concept_drift** (v16) — centroid-vs-centroid concept-drift events

### Indexes

- FTS5 virtual tables for full-text search (BM25 ranking). 4 tables
  have FTS5: `memories` (v7), `memory_chunks` (v10), `kg_entities`
  (v15), `kg_facts` (v21). Each has 3 sync triggers (ai, ad, au)
  that keep the FTS in lockstep with the source table.
- B-tree indexes on status, category, tier, timestamps
- Composite indexes for common query patterns
- Partial indexes: `idx_drift_alarms_unack` (v15, unacknowledged alarms only)
- 3 indexes on `drift_alarms` (per-memory, chronological, partial unack)
- 3 indexes on `kg_facts` for temporal queries (v18): `idx_kg_facts_validity`
  (valid_at, invalid_at), `idx_kg_facts_superseded_by`, `idx_kg_facts_event_time`

## Concurrency Model

- **Single-writer**: SQLite handles write serialization via `BEGIN IMMEDIATE`
- **Multiple readers**: WAL mode allows concurrent reads during writes
- **Background tasks**: `BEGIN IMMEDIATE` prevents double-dequeue
- **No external dependencies**: No Redis, no message queues, no daemons

## Crash safety and integrity (2026-06-22)

The 2026-06-22 technical review added the following crash-safety
properties.  All listed helpers have a regression test under
`eval/`.

| Helper / property | Module | Purpose |
|---|---|---|
| `safe_atomic_write` | `memory_common.py` | Atomic `.md` write with conflict preservation |
| `Saga(..., lock_already_held, initial_file_content)` | `save/saga.py` | File lock first; rollback detects on-disk conflict |
| `PoolExhaustedError` | `db.py` | Raised when all pooled conns are depth>0 (no infinite evict loop) |
| `find_orphan_files` / `recover_orphan_files` | `memory_integrity.py` | Detect and re-create `.md` files lost between DB upsert and file write (CLI: `--recover-orphan-files`) |
| `repair_fts_drift` | `memory_integrity.py` | Wipe + repopulate `memory_fts` from `memories` (CLI: `--repair-fts-drift`) |
| `_write_merged_markdown` | `crdt_merge.py` | Writes merged content to `.md` after every successful CRDT merge |
| `_finalize_crdt_save` | `crdt_field.py` | Same as above for the field-level (v13) CRDT path; called on all 3 return paths |
| `safe_close_db(should_commit=False)` on failure | `memory_sharing.py` | `import_shared_memory` rolls back indexer writes on any exception (SEC-3) |
| Strict CORS (no wildcard fallback) | `sync_server.py` | `MEMORY_SYNC_CORS_ORIGINS` empty = no CORS header (SEC-1) |
| Plaintext warning on non-loopback bind | `sync_server.py` | Logs at startup if `0.0.0.0`/`LAN IP` is bound without TLS (SEC-4) |
| mkdir-based lock | `cron/install_crontab.sh` | Portable single-cron-execution lock; falls back to `/tmp` if `$TMPDIR` parent missing |
| Signal handler ordering | `auto_save.py` | SIGTERM/SIGINT handlers installed **before** flock check so pre-flock daemons respond to terminate |

**Lock acquisition order** (P0-2): file lock first, then DB
connection.  Both `save_memory` and the incremental indexer
follow this order.  The saga supports a `lock_already_held`
kwarg so it doesn't double-acquire when called from the save
path.

**Connection pool eviction** (P0-3): `_evict_lru` does a
snapshot scan and tracks a `tried` set within one eviction
pass; if every conn is active (depth > 0) the call raises
`PoolExhaustedError` instead of looping forever.  Per-thread
conn keys (already in place) mitigate the cross-thread race
that motivated this fix.

**CRDT markdown sync** (Remediation #5): every successful
`crdt_save` / `crdt_field_save` write the merged content to
the `.md` file.  The markdown is the source of truth; a
stale `.md` after a merge is silent drift.  The two helpers
auto-append `.md` to `source_file` if missing (matching
the on-disk convention).

**Saga rollback cleans up dependent rows** (B-3 fix
2026-06-22 follow-up): `save.saga.undo_upsert` calls
`save.cleanup.cleanup_memory_relations()` on both the
fresh-insert and pre-existing rollback paths.  The helper
removes kg_facts, orphan kg_edges, and backlinks for the
note being rolled back.  Before this fix, a saga that failed
after an intermediate post-save hook wrote to those tables
left orphan rows.  `kg_entities` are NOT auto-deleted (shared
across notes); the `--repair-kg-orphans` CLI cleans up
entities that are truly unreferenced.

**Circuit-breaker telemetry** (2026-06-22 follow-up):
`auto_save._persist_circuit_state()` writes open/close
events to `memory_audit_log` so the breaker state is visible
across process restarts.  The new admin op
`memory_circuit_breaker_status` surfaces the events with
`limit` and `since_ts` filters.  Re-opens during cooldown
are coalesced (only the leading edge of a fresh trip logs
an open event).

**Rebuild subprocess graceful skip** (2026-06-22 follow-up):
`background_worker.handle_vec_index_rebuild` detects the
"Another vec_index rebuild is already running" message from
`rebuild_vec_index.py` and returns a graceful "skipped" string
instead of raising `RuntimeError`.  Avoids spurious task
failures when two rebuilds are scheduled simultaneously.

**Cross-process write access is single-writer by convention**
(2026-06-22 follow-up audit): no two long-lived processes
hold a write transaction on the same DB simultaneously.
Long-lived daemons (`auto_save.py daemon`, `background_worker`)
hold a `flock`; cron scripts each hold a per-cron `flock`
per `install_crontab.sh`; MCP tool invocations run inside
the opencode process.  Documented in AGENTS.md hard rule 13.

## Security

- **No network by default** — All data stays on your machine
- **No telemetry** — No data sent to external servers
- **No LLM in write path** — Deterministic extraction, no API keys needed
- **Injection detection** — `memory_injection.py` demotes suspicious content

## Surface: cron jobs, MCP tools, hooks (2026-06-22)

- **88 MCP tools** (7 CORE + 81 ADMIN). Single source of truth: `tool_registry.py`.
- **29 cron scripts** in `cron/`: `cron_embedding_recompute.py`,
  `cron_tier_migration.py`, `cron_auto_share.py`, `cron_sync.py`,
  `cron_crdt_sync.py`, `cron_heartbeat.py`, `cron_auto_summarize.py`,
  `cron_backup.py`, `cron_backup_validate.py`, `cron_compact.py`,
  `cron_concept_drift.py`, `cron_consolidate.py`, `cron_cross_session_learn.py`,
  `cron_detect_vec_drift.py`, `cron_integrity_check.py`, `cron_kg_backfill.py`,
  `cron_kg_backfill_monitor.py`, `cron_log_retention.py`, `cron_pinned_decay.py`,
  `cron_purge_auto_saves.py`, `cron_purge_expired.py`, `cron_quality_filter.py`,
  `cron_rebuild_fts.py`, `cron_retention_stats.py`, `cron_rewrite_links.py`,
  `cron_skill_extraction.py`, `cron_sync.py`, `cron_train_forget_model.py`,
  `cron_watchdog.py`, `cron_health_check.py`, `cron_purge_auto_saves.py`.
  `cron/install_crontab.sh` rewritten as an idempotent block installer
  (H-fix 2026-06-22: every cron now acquires a flock lock before running).
  Cadence reduced from `*/5` to `*/15` to avoid runaway workers.
- **6 user-facing hooks** in `hooks/`: `memory-precompact-snapshot.py`,
  `memory-proactive-context.py`, `memory-recall-session.py`,
  `memory-session-start.py`, `memory-session-end.py`,
  `memory-search-on-demand.py` + 1 log helper (`_log_error.py`).
- **Async auto-save** (2026-06-22): `auto_save.py` gains an
  async/background-batch path. The hook enqueues a tiny JSONL
  line to `<memory>/.auto_save_inbox.jsonl` (~2-5ms) and a
  long-running `auto_save.py daemon` tails the inbox and flushes
  in batches (default: 50 entries or 500ms). Per-call latency
  dropped ~95%. Set `MEMORY_ASYNC_AUTOSAVE=0` to force the legacy
  inline path. See [Async Auto-Save](#async-auto-save-2026-06-22)
  below for details.

## Async Auto-Save (2026-06-22)

The auto-save hook is the hottest path in the system: opencode
fires it on every tool call, so even small per-call overhead
compounds. To reduce that overhead, the hook uses an **inbox +
daemon** architecture:

| Component | Location | Role |
|---|---|---|
| `auto_save.py tool-complete` | the opencode hook entry | enqueue JSONL line to inbox, return `{"saved": "queued"}` in ~2-5ms |
| `<memory>/.auto_save_inbox.jsonl` | next to `memory.db` | append-only inbox (POSIX atomic appends) |
| `auto_save.py daemon` | long-running background process | tail inbox, flush batches every 500ms or 50 entries |
| `<memory>/.auto_save_daemon.pid` + lock | flock-protected | liveness check + single-daemon guarantee |

**Why**: the original sync path paid the full Python startup cost
(~100-200ms) on every call, even though the actual work was ~5ms.
The async path amortizes that startup across hundreds of saves.

**Safety properties**:

- Inbox is append-only JSONL — a daemon crash never loses data
- The daemon holds a flock so two daemons never run for the same memory dir
- PID file is checked for liveness; a stale PID triggers a clean restart
- The fast path runs allowlist/denylist/injection at enqueue time, so the
  daemon doesn't re-validate. A failure to enqueue falls back to the sync
  path so no save is ever lost.
- The daemon does a final flush on SIGTERM/SIGINT/idle timeout
  (default 1 hour of inbox silence).

**Tunables** (env vars, all optional):

- `MEMORY_ASYNC_AUTOSAVE=0` — opt out, force the sync path
- `AUTO_SAVE_BATCH_INTERVAL=0.5` — daemon flush interval (seconds)
- `AUTO_SAVE_BATCH_SIZE=50` — daemon flush size cap
- `AUTO_SAVE_DAEMON_IDLE_S=3600` — daemon exit after N seconds of silence

The first `tool_complete` call spawns the daemon as a detached
background process (no-op if one is already running). The daemon
exits on idle so the opencode session doesn't keep a zombie
process around between sessions.
