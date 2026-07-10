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
│   107 registered tools (17 CORE + 87 ADMIN + 3 DEPRECATED; 18 visible — 17 CORE + 1 memory_maintenance router)  │
│   4 user-facing hooks in hooks/ + 1 log helper module (_log_error.py) │
│   39 cron scripts (all in `cron/` subdirectory)                        │
│   ~18 CLI commands                                                      │
└──────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│ LAYER 2 — PIPELINE (Python, ~104k LOC production, ~98k LOC test)        │
│                                                                      │
│   WRITE PATH:                                                        │
│     save_pipeline.save_memory() (1,623 lines, saga → save/)         │
│       → saga (5 steps: memory, FTS5, embeddings, chunks, KG)         │
│       → audit log + cache invalidation + user_profile                │
│                                                                      │
│   READ PATH:                                                         │
│     search_pipeline.py → search/orchestrator.py (1,995 LOC)         │
│       → _expand_query (100+ synonym map)                            │
│       → 3-channel parallel: FTS5 + usearch + KG                      │
│       → _hybrid_fusion (6-factor weighted blend)                     │
│       → Qwen3-0.6B reranker (BGE-m3 fallback)                       │
│       → quality_gates (O(N log N) sliding-window near-dup dedup)     │
│                                                                      │
│   SAFETY:                                                            │
│     memory_injection (4-category prompt-injection scan, retrieval)  │
│     quality_gates (filter low-confidence results)                    │
└──────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│ LAYER 1 — STORAGE (SQLite at memory/memory.db, Schema v37)           │
│                                                                      │
│   memories / memories_fts / memory_chunks / memory_chunks_fts       │
│   memory_embeddings (BLOB, dim=256, f16, ssm_state)                  │
│   memory_vec_keys / memory_vec_idx (usearch BLOB)                    │
│   memory_field_crdt (v13) / arc_ghosts / arc_stats (v14)            │
│   kg_facts / kg_entities / kg_edges / kg_extraction_stats           │
│   backlinks (wiki-style [[link]] extraction)                         │
│   memory_audit_log / memory_ctr_feedback                             │
│   user_access_log / user_profile_access_log                          │
│   shared_memories (CRDT cross-agent pool)                            │
│   sync_log / task_queue / review_schedule / concept_drift            │
│   drift_alarms (v15) / file_mtimes / schema_version                  │
│   ~49 user-visible tables (including FTS virtual and operational)     │
└──────────────────────────────────────────────────────────────────────┘
```

## Module map (the files you'll actually touch)

| File | LOC | What it owns | When to touch |
|---|---|---|---|
| `save_pipeline.py` | 1623 | Canonical write path: memory_save, saga, safety | Touching the write path (DANGER) |
| `fact_extraction.py` | 2294 | SPO triples | Fact quality issues |
| `search_pipeline.py` | 375 | Re-export shim → search/ subpackage | Retrieval quality (real code in search/) |
| `backfill_all.py` | 816 | Audit pipeline re-export shim | Audit pipeline (real code in backfill/) |
| `mcp_maintenance.py` | 920 | All admin tool defs + memory_maintenance router | Adding an admin tool |
| `embedding_search.py` | 1245 | model2vec, usearch index, vec_keys | Vector index issues |
| `contradiction_detector.py` | 1259 | Phrase + semantic contradiction scan | Contradiction false positives |
| `sync_server.py` | 1139 | HTTP sync server with TLS + mTLS | Cross-machine sync |
| `knowledge_graph/` | 1897 | KG extraction, dedup, queries (package) | KG accuracy issues |
| `crdt_field.py` | 1110 | Per-field LWWES (v13) | Field-level CRDT bugs |
| `crdt_merge.py` | 783 | Legacy note-level LWW | Multi-agent merge bugs |
| `sync_client.py` | 656 | HTTP sync client (urllib push/pull) | Cross-machine sync |
| `memory_sharing.py` | 800 | In-DB shared memory pool | Cross-agent in-DB sharing |
| `mcp_maintenance_ops.py` | 490 | Per-operation dispatch table (~50 entries) | Adding a new admin op |
| `db_migrations.py` | 837 | Schema migrations, current v37 | Adding a new migration |
| `skill_extractor.py` | 520 | Cached skill extraction (607 rows live) | Skill cache bugs |
| `adaptive_retention.py` | 496 | Psi-formula half-life + audit_hits cache | Per-note retention scoring |
| `memory_injection.py` | 353 | 4-category prompt-injection scan | New injection patterns |
| `quality_gates.py` | 435 | O(N log N) sliding-window near-dup dedup | Quality thresholds |
| `tier_migration.py` | 471 | Hot/warm/cold tier assignment | Tier-lifecycle bugs |
| `mcp_tools.py` | 243 | Canonical tool inventory (107 registered tools) | Adding/removing a tool |
| `memory_mcp.py` | 268 | FastMCP server, tool registration | Server lifecycle, tool filtering |
| `embedding_incremental.py` | 268 | Streaming / partial-embedding state (v15) | Embedding recompute bugs |
| `arc_cache.py` | 339 | Adaptive Replacement Cache w/ ghost lists (v14) | Eviction / ghost list bugs |
| `reranker.py` | 561 | Qwen3-0.6B primary, BGE-m3 fallback | Reranker scoring issues |
| `cron/cron_*.py` | varies | 39 jobs (incl. _flock.py support module) | Adding a new cron |
| `cron/install_crontab.sh` | ~160 | Idempotent block installer for crontab | Crontab wiring changes |
| `memory_workflow.md` | 256 | System reference | Updating config / troubleshooting |
| `AGENTS.md` | 129 | Maintainer contract | Adding hard rules |

## Three rules of thumb

1. **Tag policy lives in `memory_common._resolve_tags()`.** Both `mcp_memory.py` and `auto_save._upsert_memory` route through it — don't add tag logic locally. If you need a new tag rule, add it there and both paths pick it up automatically.

2. **The 6-factor hybrid fusion weights are in `search_pipeline._RERANK_WEIGHTS`.** bm25=0.4, fitness=0.2, importance=0.15, pinned=0.1, recency=0.1, tag_match=0.05. Tunable by CTR feedback if `MEMORY_CTR_TUNING=1`. **Don't tune these without a benchmark.**

3. **Schema changes go in `migrations/NNN_name.sql` + `NNN_name.down.sql`.** Bump `SCHEMA_VERSION` in `migration_runner.py`. Current: **v37** (v13 `memory_field_crdt`, v14 `arc_ghosts`/`arc_stats`, v15 `drift_alarms` + `memory_embeddings.ssm_state`, v16 `concept_drift`, v17 `kg_cascade`, v18 `fact_temporal`, v19 kg_facts entity FKs, v20 kg_facts FTS5, v21 `kg_crdt`, v22 session memory, v23 audit_status, v24 chunk embeddings, v25 belief_plumbing, v26 belief_assertions, v27 revision_log, v28 entailment_chains, v29 graph_snapshots, v30 community_id/betweenness, v31 outbox_events, v32 scoped_outbox, v33 shared_skills, v34 entailment_validation, v35 shared_memories_target_agent_id, v36 embedding_model_tracking, v37 `cron_runs`). **Never edit the live DB schema by hand.**

4. **All writes go through `save_pipeline.save_memory(note_id=..., context=..., ...)`.** `save_memory` is the single normalization site: it derives `category`/`title_slug` from `note_id`, strips frontmatter, resolves tags via `_resolve_tags(context, ...)`, and validates content length. Callers pass raw inputs; `save_memory` does the rest. Don't re-implement any of these steps in a caller — the H1 divergence bug (2026-06-26) showed what happens when two callers each maintain their own copy.

## How to read the code

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

## Architectural invariants (break these and the system breaks)

- `memory.db` is the canonical store. Markdown is human-readable shadow.
- `memories.id` is `<category>/<slug>`. Search filters by it.
- `valid_to IS NULL` means "current." The temporal filter in `search_pipeline` relies on it.
- `kg_facts.subject/predicate/object` form the KG. Changing column shape breaks every KG query.
- `memory_vec_keys.memory_id` must be a 1:1 mapping to `memories.id` for the rebuild to work.
- The 17 CORE tool names are user-facing. Renaming breaks user expectations.

## State of the system (2026-07-10 snapshot)

Re-run before relying on these:
- 3,870+ active memories in 49 user-visible tables
- 4,431 tests collected (271 test files) at last run; 0 failures
- All features ON by default (`memory.toml`)
- 39 cron scripts (all in `cron/` subdirectory, incl. `_flock.py` support module)
- 4 user-facing hooks in hooks/ + 1 log helper module (`_log_error.py`)
- 107 registered tools: 17 CORE + 87 ADMIN + 3 DEPRECATED (routed through `memory_maintenance`)
- 30 `mcp_*.py` modules + 2 sync modules (`sync_server.py`, `sync_client.py`)
- 103,811 LOC production (all subdirs), 98,414 LOC in tests
- Schema v37, 38 migrations, ~49 tables
- `memory_skills` 607+ rows, `drift_alarms` 10+ rows, `concept_drift` 1+ row
- `task_queue` drained 12,000+ → ~300 pending
- Rule reliability: `memory-session-end.py` (Rule #7), `cron_health_check.py` (Rules #5, #9-11), `memory_compliance_check` MCP tool

If you change something substantial, update this skill and `memory_workflow.md` in the same commit.
— last reviewed 2026-07-10
