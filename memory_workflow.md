# Agentic Memory — System Reference

Internal architecture, configuration, and troubleshooting. For agent usage, see `SKILL.md`.

---

## System Overview

Local-first semantic memory for AI agents. All data at `~/.config/agentic-memory/memory/`. No cloud.

### Data Locations

| What | Where |
|------|-------|
| SQLite DB | `memory/memory.db` |
| Memory notes | `memory/<category>/<slug>.md` |
| Config (TOML) | `memory.toml` (env var overridable via `config.py`) |
| Tool registry | `tool_registry.py` — 15 CORE + 64 ADMIN (single source of truth) |
| Python env | `venv/` |
| MCP entry | `memory_mcp.py` — delegates to 25 mcp_*.py modules (79 total tools) |

### Database Tables (43 total, 29 user-visible: 26 domain + 3 FTS virtual; 14 FTS internals)

| Table | Purpose |
|-------|---------|
| `memories` | Main note storage |
| `memories_fts` | FTS5 full-text search |
| `memory_chunks` / `memory_chunks_fts` | Content chunks for long notes |
| `memory_embeddings` | Vector embeddings (dim=256, with `ssm_state` v15) |
| `memory_vec_idx` / `memory_vec_keys` | Vector similarity search (usearch HNSW) |
| `memory_skills` | Cached skill extraction |
| `memory_audit_log` | Per-call audit log |
| `kg_entities` / `kg_edges` / `kg_facts` | Knowledge graph |
| `kg_extraction_stats` | Extraction quality metrics (v12) |
| `backlinks` | Wiki-style links |
| `review_schedule` | SM-2 spaced repetition |
| `user_access_log` / `user_profile_access_log` | User profiling |
| `memory_ctr_feedback` | Search relevance feedback |
| `concept_drift` | Centroid drift events (v16, canonical SQL) |
| `drift_alarms` | Per-memory concept-drift alarms (v15) |
| `kg_facts` | SPO fact triples with v18 bi-temporal columns (event_time, valid_at, invalid_at, superseded_by, etc.) |
| `kg_facts_fts` | FTS5 index on kg_facts (v20) — ranked fact search |
| `shared_memories` / `sync_log` | Multi-agent |
| `task_queue` | Async task queue |
| `arc_ghosts` / `arc_stats` | ARC eviction cache (v14) |
| `file_mtimes` | Incremental index tracking |
| `schema_version` | Schema migrations (current: 20) |
| `memory_field_crdt` | Per-field CRDT state (v13) |

---

## Architecture

### Save Pipeline (write path)

Save steps run in order within a single transaction:
1. Markdown file write → 2. DB upsert → 3. FTS5 indexing → 4. Chunking → 5. Embedding → 6. KG extraction → 7. Fact extraction → 8. Backlinks → 9. Tier assignment → 10. Enrichment

### Search Pipeline (read path)

1. FTS5 BM25 → 2. Embedding similarity → 3. KG facts → 4. RRF fusion (k=60) → 5. Quality gates → 6. Reranking → 7. Output with fitness scores

### Hook System

All hooks fire as background subprocesses via OpenCode plugin:

| Hook | Trigger | Action |
|------|---------|--------|
| auto-save | After every tool call | `auto_save.py tool-complete` (async path, ~2-5ms) |
| session-recall | Session start | `context_monitor.py compact` (the compact subcommand is the closest match to "load context for a fresh session") |
| idle-checkpoint | Agent stops responding | `context_monitor.py idle` |
| pre-compaction | Before context compaction | `context_monitor.py compact` (the only subcommand that actually runs at pre-compaction time) |
| session-end | Session deleted | `context_monitor.py end` |

The auto-save hook uses the async/background-batch path by default
since 2026-06-22. Each call enqueues a tiny JSONL line to
`<memory>/.auto_save_inbox.jsonl` and returns
`{"saved": "queued"}`; a long-running `auto_save.py daemon` tails
the inbox and flushes in batches (default: 50 entries or 500ms).
Set `MEMORY_ASYNC_AUTOSAVE=0` to opt out and force the legacy
inline path. See `AGENTS.md` "Async Auto-Save" section for the
full architecture.

Context monitor state at `memory/sessions/.context_monitor_state.json`.

---

## Configuration

File: `memory.toml`. All features **on by default**. Set any flag to `false` to opt out.

### Feature Flags

| Flag | Default | Purpose |
|------|---------|---------|
| `knowledge_graph` | true | Entity/relation extraction + Graph-RAG |
| `temporal_tiers` | true | Hot/warm/cold tier management |
| `contextual_enrichment` | true | FTS5 overlap search on save |
| `quality_gates` | true | Validation + dedup on search |
| `summarization` | true | TF-IDF auto-summarize |
| `consolidation` | true | System 2 dedup + contradiction detection |
| `multi_agent` | true | In-process sharing pool |
| `adaptive_retention` | true | Psi-formula half-life + neural forget curve |
| `self_directed` | true | Auto-tier + heartbeat + self-healing |
| `user_profile` | true | Preference profiling |
| `forgetting_curve` | true | Ebbinghaus decay |
| `contextual_retrieval` | true | Category+tags context prepended to embeddings |
| `late_interaction` | true | Late interaction reranking |
| `fts5_cache` | true | FTS5 LRU result cache |
| `saga_enabled` | true | Transactional write path |
| `reranker_disabled` | false | Qwen3-0.6B / BGE-m3 (set true to disable) |
| `crdt_enabled` | true | Version vector tracking + conflict resolution |

### Search Parameters

| Param | Default | Description |
|-------|---------|-------------|
| `deep_rerank` | false | Use reranker (Qwen3-0.6B / BGE-m3) |
| `include_global` | true | Search both local + global with RRF |
| `include_invalid` | true | Include superseded/expired notes |
| `rerank` | true | Apply reranking step |
| `boost_pinned` | true | Boost pinned notes |
| `recency_weight` | 0.1 | Recency weight in scoring |

### Chunking

`chunk_threshold`: 2000 chars · `max_chunk_size`: 1200 chars

---

## Troubleshooting

### Health Checks
- `memory_maintenance(operation="check_integrity")` — full DB integrity
- `memory_maintenance(operation="backfill_all", mode="health")` — FTS5 health
- `python3 rebuild_vec_index.py --dry-run` — vector index health
- `memory_maintenance(operation="auto_save_status")` — auto-save health
- `python memory_integrity.py <db>` — file/DB drift + FTS5 + orphan check
- `python memory_integrity.py <db> --recover-orphan-files [--dry-run]`
  — Scenario 7 fix (2026-06-22): re-create .md files for memories
  whose file is missing on disk (saga crashed between DB upsert
  and file write).  Regenerates the .md from the DB content.
- `python memory_integrity.py <db> --repair-fts-drift [--dry-run]`
  — Scenario 11 fix (2026-06-22): run the FTS5 rebuild to repair
  drift between `memories` and `memory_fts`.  Wipes + repopulates
  from the source table (works for content FTS5 where the standard
  `REBUILD` command doesn't re-read source).
- `python memory_integrity.py <db> --repair-kg-orphans [--dry-run]`
  — B-3 fix (2026-06-22 follow-up): delete orphan rows in
  `kg_edges`, `kg_entities`, and `backlinks`.  Saga rollbacks
  and pre-fix hard_delete_note calls can leave orphan rows
  in these tables.  Use `--dry-run` to preview.
- `memory_maintenance(operation="circuit_breaker_status", limit=20, since_ts=None)`
  — 2026-06-22 follow-up: returns the last N auto-save circuit-breaker
  open/close events from `memory_audit_log`.  Persisted since
  session 2 follow-up via `auto_save._persist_circuit_state`.
  Useful for cross-restart visibility (in-memory state is lost
  on process exit; the audit log persists).

### Rebuild Indexes
- `memory_maintenance(operation="rebuild")` — rebuild FTS5
- `python3 rebuild_vec_index.py` — rebuild vector index
- `memory_maintenance(operation="backfill_all", mode="full")` — full backfill

### Check Data Counts
- `memory_maintenance(operation="tier_stats")` — tier distribution
- `memory_maintenance(operation="arc_stats")` — KG stats
- `memory_maintenance(operation="review_schedule")` — spaced repetition

### Common Issues

| Symptom | Fix |
|---------|------|
| `memory_search` returns empty | `memory_maintenance(operation="rebuild")` |
| Vector search broken | `python3 rebuild_vec_index.py` |
| Auto-saves not in DB | Check `auto_save_status`; run `backfill_all` |
| Orphaned FTS entries | Normal for superseded notes |
| Jina SIGSEGV | Historical issue with deprecated Jina. Current Qwen3-0.6B / BGE-m3 are MPS-safe. Set `reranker_disabled = true` if seen. |

---

## Automated Maintenance

26 scheduled jobs (23 `cron/cron_*.py` scripts plus the `background_worker.py`, `auto_save.py daily-digest`, and `backfill_all.py --incremental` entries in the crontab). Install: `bash cron/install_crontab.sh` (idempotent, marker-delimited).

| Component | Schedule | What it does |
|-----------|----------|--------------|
| `background_worker.py` | Every 15 min | Task queue + vec drift auto-rebuild |
| `cron_auto_summarize.py` | Daily | Summarize long notes |
| `cron_auto_share.py` | Daily | Auto-publish to shared pool |
| `cron_backup.py` | Daily 02:00 | SQLite backup (7 daily) |
| `cron_compact.py` | Monthly 1st | Full rebuild: FTS, vec, KG, cross-session |
| `cron_concept_drift.py` | Sunday 06:00 | Centroid drift detection |
| `cron_consolidate.py` | Sunday 04:00 | Dedup + contradiction detection |
| `cron_cross_session_learn.py` | Monday 04:15 | Cross-session learning |
| `cron_detect_vec_drift.py` | Daily 04:30 | Vec drift detection |
| `cron_heartbeat.py` | Daily 03:00 | Tier + importance + archive |
| `cron_integrity_check.py` | Sunday 01:00 | DB health + FTS consistency |
| `cron_kg_backfill.py` | Sunday 03:30 | KG backfill |
| `cron_pinned_decay.py` | Sunday 05:00 | Unpin stale pinned notes |
| `cron_purge_expired.py` | Monthly 1st 06:30 | Hard-delete tombstones >30d |
| `cron_rebuild_fts.py` | Daily 02:33 | Lightweight FTS5 rebuild |
| `cron_retention_stats.py` | Monday 08:00 | Adaptive retention + neural forget |
| `cron_rewrite_links.py` | Sunday 04:30 | Fix broken wiki-links |
| `cron_skill_extraction.py` | Monday 03:45 | Refresh skill cache |
| `cron_crdt_sync.py` | Hourly :15 | Multi-peer CRDT sync (staggered 10 min after `cron_sync`) |
| `cron_embedding_recompute.py` | Daily 04:00 | Re-embed after model revision change |
| `cron_sync.py` | Hourly :05 | Single-peer two-way sync |
| `cron_tier_migration.py` | Sunday 03:00 | On-demand hot/warm/cold tier migration |
| `auto_save.py daily-digest` | Daily 00:00 | Roll auto-saves into daily summaries |

> **Note:** `cron_log_retention.py` and `cron_purge_auto_saves.py` are not
> scheduled in `install_crontab.sh` — they exist for manual invocation
> only (e.g. `python cron/cron_purge_auto_saves.py --days 90`). The full
> scheduled-cron picture is in `cron/install_crontab.sh`.

---

## Deprecated

| Script | Status | Replacement |
|--------|--------|-------------|
| `agent_init.py` | Deprecated | session-recall hook |
| `session_reflect.py` | Deprecated | session-end hook |
| `cron_worker.py` | Removed 2026-06-17 | `background_worker.py` |
| `cron_daily_digest.py` | Removed 2026-06-17 | `auto_save.py daily-digest` |
| `cron_temporal_summarize.py` | Removed 2026-06-17 | `memory_temporal_diff` + `cron_consolidate.py` |
