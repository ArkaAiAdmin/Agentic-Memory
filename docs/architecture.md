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

The search orchestrator (`search_memories` in `search/orchestrator.py`)
runs the following **12 phases** in order:

> **Pipeline flow:** Input normalization & query type detection → FTS5 BM25 retrieval → Vector (usearch) retrieval → ColBERT late-interaction retrieval → Reciprocal Rank Fusion (RRF) merge → Cross-encoder reranking (optional) → Temporal decay application → Neural forget curve adjustment → KG concept/centrality boost → Final score computation & ranking → Result envelope construction → Error counter & latency logging

1. Input normalization & query type detection
2. FTS5 BM25 retrieval
3. Vector (usearch) retrieval
4. ColBERT late-interaction retrieval
5. Reciprocal Rank Fusion (RRF) merge
6. Cross-encoder reranking (optional)
7. Temporal decay application
8. Neural forget curve adjustment
9. KG concept/centrality boost
10. Final score computation & ranking
11. Result envelope construction
12. Error counter & latency logging

## Save Pipeline

The canonical write path (`save_memory` → `_upsert_memory_row` +
`_run_post_save_hooks`) runs the following **13 steps** in order:

1. Lock acquire
2. Compute tier + PRAGMA setup
3. Upsert memory row (DB + tier inline)
4. CRDT version bump (legacy; gated by `legacy_note_crdt` flag)
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
agentic-memory/                    # Repo root
├── agentic_memory/                # Python package (pip installable; 2 files)
│   ├── __init__.py                 # Re-exports Memory, AgentMemory, main
│   └── __main__.py                 # python -m agentic_memory
├── cli.py                          # 11 CLI entry points
├── memory_mcp.py                   # MCP server (thin orchestrator)
├── save_pipeline.py                # Write path shim → save/
├── save/                           # Write path subpackage
│   ├── __init__.py                 # Public API
│   ├── crdt_helpers.py             # CRDT snapshot extraction
│   ├── indexers.py                 # FTS/embedding/chunk index writes
│   ├── backlinks.py                # Auto-backlink computation
│   └── post_save_hooks.py          # Fitness recalc, tier, audit
├── search_pipeline.py              # Read path shim → search/
├── search/                         # Read path subpackage
│   ├── __init__.py                 # Public API
│   ├── query_parser.py             # Query type detection, expansion, FTS
│   ├── rerankers.py                # Cross-encoder, late interaction
│   ├── scoring.py                  # RRF fusion, temporal decay, CTR
│   ├── synthesis.py                # BB1/BB2 synthesis
│   ├── chunk_index.py              # Chunk search, Graph-RAG expansion
│   ├── instrumentation.py          # Timing/log/observability
│   └── orchestrator.py             # search_memories + 12-phase search
├── backfill_all.py                 # Audit pipeline shim → backfill/
├── backfill/                       # Audit pipeline subpackage
│   ├── __init__.py                 # Public API
│   ├── index_backfills.py          # FTS, embedding, chunk, vec backfills
│   └── kg_backfills.py             # KG facts, entity filter
├── auto_save.py                    # Shim + CLI entry; impl in background/auto_save.py
├── background/                     # Auto-save + worker
│   ├── auto_save.py                # Core auto-save logic
│   ├── background_worker.py        # Task queue worker (flock-protected)
│   └── ...
├── knowledge_graph.py              # Entity extraction
├── fact_extraction.py              # SPO triple extraction
├── kg_dedup.py                     # Entity deduplication
├── contradiction_detector.py       # Conflict detection
├── background_queue.py             # SQLite-backed task queue
├── embedding_search.py             # Semantic search via model2vec
├── memory_common.py                # Shared utilities (connection pool, flock)
├── db.py                           # Connection pool with tenant routing
├── migration_runner.py             # Schema migrations (current v37)
└── ... (123 modules total)
```

| Module | Layer | Purpose |
|--------|-------|---------|
| `save_pipeline.py` + `save/` | Write | Orchestrates markdown → index writes |
| `search_pipeline.py` + `search/` | Read | BM25 + vector + KG hybrid search |
| `backfill_all.py` + `backfill/` | Maintenance | Audit pipeline for index rebuilds |
| `auto_save.py` + `background/` | Hook | Async inbox + daemon auto-save |
| `knowledge_graph.py` | Write | Pattern-based NER, entity storage |
| `fact_extraction.py` | Write | Regex-based SPO triple extraction |
| `kg_dedup.py` | Maintenance | Exact + semantic entity dedup |
| `contradiction_detector.py` | Quality | Conflicting fact detection |
| `background_queue.py` | Infra | SQLite-backed async task queue |
| `background_worker.py` | Infra | Task queue worker (flock-protected) |
| `embedding_search.py` | Search | model2vec semantic search |
| `memory_injection.py` | Safety | Prompt injection detection |
| `migration_runner.py` | Infra | Schema migrations (v37, 39 migrations) |

## Surface: MCP tools, cron jobs, hooks

- **104 MCP tools** (17 CORE + 87 ADMIN).
  Single source of truth: `tool_registry.py`.
- **39 cron scripts** in `cron/` — task queue, FTS rebuild, tier migration,
  kg backfill, integrity check, heartbeat, consolidation, etc.
  Cadence: `*/15 min`. Each cron acquires a `flock` before running.
- **6 lifecycle hooks** in `hooks/` — session start/end,
  precompact snapshot, proactive context, recall,
  search-on-demand. See `~/.claude/settings.json` and `opencode.jsonc` for wiring.
  `_log_error.py` is a log helper, not a lifecycle hook.

## Concurrency Model

- **Single-writer**: SQLite handles write serialization via `BEGIN IMMEDIATE`
- **Multiple readers**: WAL mode allows concurrent reads during writes
- **Background tasks**: `BEGIN IMMEDIATE` prevents double-dequeue
- **No external dependencies**: No Redis, no message queues, no daemons required
  (the optional background daemon is graceful-degradable)

## Feature Flags

See `memory.toml [features]` for all flags. Key defaults:

| Flag | Default | Purpose |
|------|---------|---------|
| `crdt_enabled` | `true` | Version vector tracking + conflict resolution |
| `legacy_note_crdt` | `false` | Legacy note-level VV bump (deprecated; per-field CRDT is source of truth) |
| `temporal_tiers` | `true` | Hot/warm/cold tier management |
| `adaptive_retention` | `true` | Psi formula + spaced repetition |
| `feature_temporal_kg` | `true` | Fact-level temporal KG (event_time, supersession, invalidation) |
| `saga_enabled` | `true` | Transactional save (DB + vec + file) |
| `vec_rebuild_adaptive` | `true` | Dynamic vec-index rebuild threshold based on write velocity |
| `summarization` | `true` | Auto-summarize long notes |
| `user_profile` | `true` | Personalize recall ranking from access history |
| `consolidation` | `true` | SHA-256 + n-gram Jaccard dedup |
| `quality_gates` | `true` | Filter results below relevance threshold |

## Safety & Integrity

- **Lock order**: file flock first, then DB conn. Both `save_memory` and
  the incremental indexer follow this order.
- **Saga rollback**: `save.saga.undo_upsert` calls
  `save.cleanup.cleanup_memory_relations()` — removes kg_facts,
  orphan kg_edges, and backlinks on rollback.
- **Atomic markdown writes**: `safe_atomic_write` preserves conflicting
  on-disk versions as `<path>.conflict-<pid>-<ts>`.
- **Circuit breaker**: `auto_save` uses a circuit breaker for repeated
  failures; state is persisted to `memory_audit_log`.
- **CRDT markdown sync**: Every successful merge writes the merged
  content to the `.md` file. Markdown is source of truth; stale `.md`
  after a merge is silent drift.
- **Connection pool**: per-DB-path pool with re-entrancy guard;
  per-thread keys; `PoolExhaustedError` on full depth.

---
*This file is generated by `scripts/generate_architecture_md.py`.
Do not edit directly; run the script and review the diff.*
