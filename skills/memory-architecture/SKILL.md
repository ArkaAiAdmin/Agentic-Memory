---
name: memory-architecture
description: Maintainer view of the agentic-memory system. Use when onboarding to the codebase, planning a refactor that crosses module boundaries, or answering "what calls what / what depends on what". Don't use for end-user MCP tool questions — that's the user-facing `agentic-memory` skill.
---

# Memory Architecture (Maintainer)

A one-page mental model of the agentic-memory system, sized for someone about to change it.

## The one-liner

**Markdown files are the source of truth. SQLite is a derived index that can be rebuilt from them. The agent sees the system through MCP tools + hooks; the maintainer sees it through Python modules + cron jobs.**

## The three layers

```
┌──────────────────────────────────────────────────────────────────────┐
│ LAYER 3 — SURFACE                                                     │
│   78 MCP tools (15 CORE in mcp_tools.py; 63 ADMIN grouped under       │
│   `memory_maintenance(operation=...)` in mcp_tools.py; 8 added        │
│   on 2026-06-22)                                                     │
│   4 Claude Code hooks in hooks/                                      │
│   23 cron jobs in cron/ (moved from repo root 2026-06-22)            │
│   10 CLI commands (cli.py) + 1 agentic-memory sub-CLI (P1-4)         │
└──────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│ LAYER 2 — PIPELINE (Python, 44,193 LOC at top level)                  │
│                                                                      │
│   WRITE PATH:                                                        │
│     save_pipeline.save_memory() (1,050 lines, shim → save/)         │
│       → saga (5 steps: memory, FTS5, embeddings, chunks, KG)         │
│       → audit log + cache invalidation + user_profile                │
│       → _recalculate_fitness_scores(conn=...) atomic with caller     │
│                                                                      │
│   READ PATH:                                                         │
│     search_pipeline.search_memories() (387 lines shim → search/)    │
│       → _expand_query (100+ synonym map)                            │
│       → 3-channel parallel: FTS5 + usearch + KG                      │
│       → _hybrid_fusion (6-factor weighted blend)                     │
│       → Qwen3-0.6B reranker (BGE-m3 fallback)                       │
│       → quality_gates (O(N log N) sliding-window near-dup dedup)     │
│       → briefing                                                     │
│                                                                      │
│   NEURAL LAYER:                                                      │
│     embedding_search (model2vec potion-base-8M, dim=256, f16)         │
│     reranker (Qwen3-0.6B primary, BGE-m3 fallback)                   │
│                                                                      │
│   KNOWLEDGE LAYER:                                                   │
│     fact_extraction → kg_dedup (exact + semantic) → contradiction_detector
│                                                                      │
│   MULTI-AGENT:                                                       │
│     crdt_field (per-field LWWES, v13) + crdt_merge (legacy note-LWW)│
│     sync_server / sync_client (HTTP + native TLS + mTLS)             │
│     memory_sharing (in-DB shared_memories pool with TTL,             │
│                     auto_share opt-in via MEMORY_AUTO_SHARE)         │
│                                                                      │
│   MAINTENANCE:                                                       │
│     arc_cache (Adaptive Replacement Cache with ghost lists, v14)     │
│     tier_migration (hot/warm/cold, on-demand + cron)                  │
│     embedding_incremental (streaming / partial embeddings, v15)      │
│     skill_extractor (cached procedural knowledge, 607 rows live)     │
│                                                                      │
│   SAFETY:                                                            │
│     memory_injection (4-category prompt-injection scan, retrieval)  │
│     quality_gates (filter low-confidence results)                    │
└──────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│ LAYER 1 — STORAGE (SQLite at memory/memory.db, ~63 MB at 3.9K mems)  │
│                                                                      │
│   memories (canonical rows; valid_from/valid_to/superseded_by)      │
│   memories_fts (FTS5 virtual table)                                  │
│   memory_embeddings (BLOB, dim=256, f16, ssm_state v15)               │
│   memory_chunks / memory_chunks_fts (chunked long notes)             │
│   memory_vec_keys / memory_vec_idx (usearch BLOB, ~3 MB)             │
│   memory_field_crdt (v13, per-field CRDT state)                      │
│   memory_skills (cached skill extraction, 607 rows live)            │
│   memory_audit_log (every MCP call: ts, tool, args, latency, error)  │
│   kg_facts / kg_entities / kg_edges / kg_extraction_stats            │
│   backlinks (wiki-style [[link]] extraction)                         │
│   memory_ctr_feedback (clicks/dismissals for ranking)                │
│   user_access_log / user_profile_access_log                          │
│   shared_memories (CRDT cross-agent pool, auto_share opt-in)         │
│   sync_log (sync server event log)                                   │
│   task_queue (background work, ~297 pending)                        │
│   review_schedule (SM-2 spaced repetition)                           │
│   concept_drift (centroid drift events)                              │
│   drift_alarms (v15, per-memory alarms, 10 rows live)                │
│   arc_ghosts / arc_stats (v14, ARC ghost lists)                      │
│   file_mtimes (per-source-file mtime tracking)                       │
│   schema_version (current: 15)                                       │
│                                                                      │
│   27 user-visible tables total (excluding FTS5 shadow tables)         │
└──────────────────────────────────────────────────────────────────────┘
```

## Module map (the files you'll actually touch)

| File | LOC | What it owns | When to touch |
|---|---|---|---|
| `mcp_tools.py` | ~230 | Canonical tool inventory — re-exports all 70 MCP tools | Adding/removing a tool |
| `memory_mcp.py` | ~1200 | FastMCP server, tool registration, async wrappers | Server lifecycle, tool filtering |
| `save_pipeline.py` | 1050 | Canonical write path: memory_save, saga, safety | Touching the write path (DANGER: see H1) |
| `search_pipeline.py` | 387 | Re-export shim for the search/ subpackage | Touching retrieval quality (real code is in search/) |
| `backfill_all.py` | 764 | Audit pipeline re-export shim | Audit pipeline changes (real code is in backfill/) |
| `mcp_maintenance.py` | ~1200 | All maintenance/admin tool definitions + memory_maintenance router | Adding an admin tool |
| `mcp_maintenance_ops.py` | ~450 | Per-operation dispatch table (~50 entries) | Adding a new admin op |
| `embedding_search.py` | ~1200 | model2vec, usearch index, vec_keys | Vector index issues |
| `reranker.py` | 560 | Qwen3-0.6B primary, BGE-m3 fallback | Reranker scoring issues |
| `knowledge_graph.py` | 1655 | KG extraction, dedup, queries | KG accuracy issues |
| `fact_extraction.py` | ~600 | SPO triples | Fact quality issues |
| `contradiction_detector.py` | ~1300 | Phrase + semantic contradiction scan | Contradiction false positives |
| `crdt_merge.py` | 584 | Legacy note-level LWW; delegates to crdt_field when present | Multi-agent merge bugs (v13+) |
| `crdt_field.py` | ~400 | Per-field LWWES (v13) | Field-level CRDT bugs |
| `arc_cache.py` | ~350 | Adaptive Replacement Cache with ghost lists (v14) | Eviction / ghost list bugs |
| `sync_server.py` | 594 | HTTP sync server with native TLS + mTLS | Cross-machine sync |
| `sync_client.py` | 473 | HTTP sync client (urllib-based push/pull) | Cross-machine sync |
| `memory_sharing.py` | 504 | In-DB shared memory pool (was multi_agent.py) | Cross-agent in-DB sharing |
| `adaptive_retention.py` | 350 | Psi-formula half-life + module-level audit_hits cache | Per-note retention scoring |
| `memory_injection.py` | 353 | 4-category prompt-injection scan | New injection patterns |
| `quality_gates.py` | 345 | O(N log N) sliding-window near-dup dedup (P1-5) | Quality thresholds |
| `tier_migration.py` | 326 | Hot/warm/cold tier assignment (powers `memory_run_tier_migration`) | Tier-lifecycle bugs |
| `embedding_incremental.py` | ~250 | Streaming / partial-embedding state (v15, ssm_state) | Embedding recompute bugs |
| `skill_extractor.py` | ~600 | Cached skill extraction (607 rows live) | Skill cache bugs |
| `db_migrations.py` | ~600 | Schema migrations, current v15 | Adding a new migration |
| `cron/cron_*.py` | varies | 23 background jobs (moved from repo root 2026-06-22) | Adding a new cron |
| `cron/install_crontab.sh` | ~160 | Idempotent block installer for the crontab | Crontab wiring changes |
| `memory_workflow.md` | ~430 | System reference | Updating config / troubleshooting docs |
| `AGENTS.md` | ~395 | Maintainer contract | Adding hard rules |

## Three rules of thumb

1. **If you change `save_pipeline.save_memory`, mirror it in `auto_save._upsert_memory` and `auto_save.tool_complete`.** The hook path and the canonical path diverge (H1 in the audit). They share a saga in theory, not in code. **Most dangerous bug class in the system.**

2. **The 6-factor hybrid fusion weights are in `search_pipeline._RERANK_WEIGHTS`.** bm25=0.4, fitness=0.2, importance=0.15, pinned=0.1, recency=0.1, tag_match=0.05. Tunable by CTR feedback if `MEMORY_CTR_TUNING=1`. **Don't tune these without a benchmark** — there is no published LoCoMo number for the current weights, so any change is unfalsifiable without one.

3. **Schema changes go in `migrations/NNN_name.sql` + `NNN_name.down.sql`.** Bump `SCHEMA_VERSION` in `migration_runner.py`. Migrations are auto-discovered from the directory by `_get_available_migrations()` — there is no `MIGRATIONS = [...]` list to maintain. Current is v16 (v13 added `memory_field_crdt`, v14 added `arc_ghosts`/`arc_stats`, v15 added `drift_alarms` and `memory_embeddings.ssm_state`, v16 moved `concept_drift` from a Python helper into a numbered `.sql` migration for consistency with every other table). **Never edit the live DB schema by hand.**

## How to read the code

For a one-shot answer to "what does X call":

```bash
# Find what calls a function
grep -rn "memory_save\b" --include="*.py" . | head

# Find what implements a tool
grep -rn "@mcp.tool" --include="*.py" . | head

# Find what writes to a table
grep -rn "INSERT INTO memories" --include="*.py" .

# Find what reads from a table
grep -rn "SELECT.*FROM memories" --include="*.py" .
```

For deeper exploration, the `explore` subagent is best.

## Architectural invariants (the things that, if changed, break the system)

- `memory.db` is the canonical store. Markdown is human-readable shadow.
- The `memories.id` format is `<category>/<slug>`. Don't break this — search filters by it.
- `valid_to IS NULL` means "current." The temporal filter in `search_pipeline` relies on it.
- `kg_facts.subject/predicate/object` form the KG. Changing the column shape breaks every KG query.
- `memory_vec_keys.memory_id` must be a 1:1 mapping to `memories.id` for the rebuild to work.
- The 15 CORE tool names are user-facing. Renaming breaks user expectations.

## State of the system (2026-06-22 snapshot)

Re-run before relying on these:
- 3,870 active memories in 27 user-visible tables
- 2,711 tests pass, 0 fail, 10 skip, 0 errors (142 test files, ~2,720 test functions)
- All 17 features ON by default (`memory.toml`)
- 23 crons (was 19; 4 new on 2026-06-22), all in `cron/` subdirectory
- 4 hooks, all writing to STDOUT (post 2-day debugging fix; `_log_error.py` redirects hook errors to a log file)
- 78 MCP tools: 15 CORE + 63 ADMIN (routed through `memory_maintenance`)
- 20 `mcp_*.py` modules + 2 sync modules (`sync_server.py`, `sync_client.py`)
- 44,193 LOC at top level, 58,642 LOC in tests
- Schema v15 (v13 added `memory_field_crdt`, v14 added `arc_ghosts`/`arc_stats`, v15 added `drift_alarms` + `memory_embeddings.ssm_state`)
- `memory_skills` populated with 607 rows, `drift_alarms` 10 rows, `concept_drift` 1 row
- `task_queue` drained 12,026 → ~297 pending
- `save_pipeline.py` recovered from a 2026-06-21 backup regression (was 0 bytes; now 1,050 lines)
- `agentic_memory/` SDK is `pip install -e .`-installable (P1-4)
- `quality_gates.filter_results` rewritten as O(N log N) sliding-window near-dup dedup (P1-5)
- 9 new MCP tools on 2026-06-22: `memory_list_drift_alarms`, `memory_arc_reset`, `memory_run_tier_migration`, `memory_check_embedding_model`, `memory_incremental_update`, `memory_merge_embeddings`, `memory_extract_skills`, `memory_list_skills`, `memory_auto_share`
- CRDT audit: both v13 field-level and legacy note-level paths work concurrently
- Sync server supports native TLS via `MEMORY_SYNC_TLS_CERT`/`MEMORY_SYNC_TLS_KEY` (mTLS via `MEMORY_SYNC_TLS_CLIENT_CA`)
- `_recalculate_fitness_scores` takes a `conn` parameter for atomic saga commits
- Mypy: 0 errors (all 141 fixed on 2026-06-20)

If you change something substantial, update this skill and `memory_workflow.md` in the same commit.

— last reviewed 2026-06-22
