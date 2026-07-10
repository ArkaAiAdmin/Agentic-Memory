# MCP Tools Reference

Agentic Memory exposes **17 CORE + 87 ADMIN + 3 DEPRECATED = 107 total registered names** (104 `@mcp.tool()` registrations, with DEPRECATED tools excluded from the direct surface). The single source of truth for the tool surface is `tool_registry.py` (`CORE_TOOLS`, `ADMIN_TOOLS`, and `DEPRECATED` lists).

## Core Tools (17)

The 17 tools most agents use day-to-day. Each is a first-class MCP
function; no grouping required.

### `memory_save`

Save a memory to the system.

```python
memory_save(
    content="Always use WAL mode for SQLite",
    category="lessons",
    title_slug="sqlite-wal-mode",
    tags=["sqlite", "database"],
    is_global=False,
    pinned=False,
    importance=3,
)
```

**Parameters:**
- `content` (str, required) — Memory content (markdown supported)
- `category` (str, required) — One of: lessons, decisions, projects, preferences, quirks, sessions
- `title_slug` (str, optional) — URL-safe identifier (auto-generated if not provided)
- `tags` (list[str], optional) — Tags for categorization
- `is_global` (bool, optional) — Save to global memory (default: false)
- `pinned` (bool, optional) — Pin to prevent auto-archival (default: false)
- `importance` (int, optional) — 1-5, default 3

**Returns:** Memory ID (str)

---

### `memory_search`

Search memories using hybrid BM25 + semantic search.

```python
memory_search(
    query="SQLite concurrency patterns",
    limit=10,
    include_global=True,
    rerank=True,
    deep_rerank=False,
    skill_first=False,
)
```

**Parameters:**
- `query` (str, required) — Search query
- `limit` (int, optional) — Maximum results (default: 5)
- `include_global` (bool, optional) — Include global memories (default: true)
- `rerank` (bool, optional) — Enable cross-encoder reranking (default: true)
- `deep_rerank` (bool, optional) — Use Qwen3-0.6B / BGE-m3 deep reranker (default: false)
- `skill_first` (bool, optional) — Try the skills cache before FTS5 (default: false)

**Returns:** Dict with `results` (list of result dicts), `count`,
`output` (human-readable text), `query_id`, optionally `synthesis`.

---

### `memory_recall`

Fast query-based context matching. Returns recent memories and
context relevant to the query.

```python
memory_recall(query="SQLite concurrency", limit=5)
```

---

### `memory_delete`

Soft-delete a memory. Can be restored within 30 days.

```python
memory_delete(note_id="lessons/sqlite-wal-mode", hard=False)
```

**Parameters:**
- `note_id` (str, required) — Memory ID
- `hard` (bool, optional) — Immediately purge (default: false, soft-delete)

---

### `memory_note`

Read, update, patch, supersede, or restore a specific note by ID.

```python
memory_note(note_id="lessons/sqlite-wal-mode", action="read")
```

---

### `memory_learn`

Fast-path memory save with automated slug generation and
auto-categorization. Ideal for lessons and insights.

```python
memory_learn(content="Always use WAL mode for SQLite", category="lessons")
```

---

### `memory_audit`

View recent memory activity, errors, and system health.

```python
memory_audit(hours=24, limit=20)
```

---

### `memory_organize`

Run safe maintenance batch (compact, consolidate, link rewrite).

```python
memory_organize(target="safe_default", dry_run=False)
```

---

### `memory_share`

Share memories with other agents or view the shared pool.

```python
memory_share(note_id="lessons/sqlite-wal-mode", share_with="agent-b")
```

---

### `memory_graph`

Explore the knowledge graph: traverse entities, find paths, or
return graph statistics.

```python
memory_graph(query="SQLite", action="explore")
```

---

### `memory_profile`

View user/agent profile, ARC stats, and cached skills.

```python
memory_profile(action="stats")
```

---

### `memory_session_start`

Boot a session: returns recent memories, profile state, and any
active context. Designed to be called by hooks on session start.

```python
memory_session_start(query="my-app")
```

---

### `memory_advanced`

Power user escape hatch — pass through to any memory_maintenance
operation. Use when no CORE verb covers your use case.

```python
memory_advanced(operation="tier_stats", kwargs="{}")
```

---

### `memory_review_beliefs`

Review low-confidence, old, or stale belief assertions that may
need agent attention.

```python
memory_review_beliefs(min_confidence=0.5, older_than_days=30, limit=20)
```

---

### `memory_curate_autosave`

Review auto-saved tool invocations and promote or discard them.

```python
memory_curate_autosave(action="list")
```

---

### `memory_health_check`

Unified health-check returning DB, index, worker, and schema
status across all subsystems.

```python
memory_health_check()
```

---

### `memory_system_health`

Comprehensive green/yellow/red health check with actionable next
steps across 6 dimensions (DB, search, worker, crons, auto-save,
disk).

```python
memory_system_health()
```

---

## Admin Tools (87)

All admin operations go through the `memory_maintenance` grouped
tool, dispatched by `operation=`. The full list (single source of
truth: `tool_registry.ADMIN_TOOLS`):

| Tool | Purpose |
|------|---------|
| `memory_maintenance` | The dispatcher itself — pass `operation=...` to call any admin op |
| `memory_adaptive_retention` | Compute the psi-formula half-life for a memory |
| `memory_admin_policy_hash` | Get or refresh the drift-policy hash |
| `memory_agent_clear` | Clear the local CRDT agent identity |
| `memory_agent_init` | Initialize a CRDT agent identity for the local host |
| `memory_agent_list` | List known CRDT agent identities |
| `memory_arc_reset` | Reset ARC ghost lists and stats (operator escape hatch) |
| `memory_arc_stats` | Read ARC eviction-cache stats without recomputing |
| `memory_audit_query` | Query the per-call audit log with filters |
| `memory_auto_save_daemon_metrics` | Show auto-save daemon performance metrics |
| `memory_auto_save_hook` | Programmatic equivalent of the opencode tool-complete hook |
| `memory_auto_save_status` | Show auto-save health, last batch, daemon PID |
| `memory_auto_share` | Auto-publish opt-in memories to the shared pool |
| `memory_auto_summarize` | Trigger TF-IDF summarization on a note |
| `memory_backfill_all` | Run the audit pipeline (FTS, vec, KG, etc.) |
| `memory_background_task_status` | Check status of a background task |
| `memory_check_concept_drift` | Compute centroid-vs-centroid drift for an embedding |
| `memory_check_contradictions` | Scan memories for conflicting facts |
| `memory_check_embedding_model` | Verify active embedding model revision |
| `memory_check_integrity` | Full DB integrity check |
| `memory_circuit_breaker_status` | Show circuit breaker open/closed state |
| `memory_compact` | Run deduplication and consolidation |
| `memory_compile_skill` | Compile a lesson into an executable agent skill |
| `memory_compliance_check` | Audit session compliance with reliability rules |
| `memory_consolidate` | Run fact consolidation + contradiction detection |
| `memory_crdt_status` | Show CRDT version-vector state for the local agent |
| `memory_crdt_sync` | Push/pull CRDT state with a peer |
| `memory_daily_digest` | Roll auto-saves into a daily summary note |
| `memory_dashboard` | Return a high-level summary (counts, health) |
| `memory_dedup` | Find and merge near-duplicate memories |
| `memory_detect_contradictions` | Force a contradiction scan over a category |
| `memory_extract_skills` | Refresh the skills cache from existing lessons |
| `memory_facts_list` | List extracted SPO triples with filters |
| `memory_facts_search` | Search the knowledge-graph facts table |
| `memory_facts_stats` | Statistics on the facts table |
| `memory_graph_evolution` | Show how the KG has changed over time |
| `memory_graph_insights` | Discover patterns and anomalies in the KG |
| `memory_graph_search` | Walk the KG from a starting entity |
| `memory_graph_shortest_path` | Find the shortest path between two entities |
| `memory_graph_stats` | Statistics on the KG (entities, edges, density) |
| `memory_graph_traverse` | Walk the KG from a starting entity by edge type |
| `memory_heartbeat` | Run the periodic self-healing + tier sweep |
| `memory_incremental_update` | Re-index a single memory (FTS, vec, chunk, KG) |
| `memory_ingest` | Read a file or fetch a URL and save as memory |
| `memory_list_drift_alarms` | List per-memory concept-drift alarms |
| `memory_list_federated_skills` | List skills from federated sources |
| `memory_list_skills` | List cached skills ordered by hit count |
| `memory_list_threads` | List active session threads |
| `memory_llm_unload` | Force-unload the LLM from memory to release GPU |
| `memory_metrics_server` | Start the Prometheus-format metrics endpoint |
| `memory_okf_export` | Export memories to OKF (one .md per memory) |
| `memory_okf_import` | Import memories from an OKF directory |
| `memory_pinned_decay_check` | Find pinned notes that haven't been accessed in N days |
| `memory_profile_access` | Record a profile-relevant access event |
| `memory_profile_stats` | Read user-profile hit counts and top categories |
| `memory_purge_auto_saves` | Hard-delete auto-saves older than N days |
| `memory_purge_expired` | Hard-delete tombstoned notes older than 30 days |
| `memory_quality_filter` | Apply the quality_gates filter to a result set |
| `memory_quality_stats` | Show quality-gate pass/fail rates |
| `memory_rebuild` | Rebuild the search index from markdown files |
| `memory_recall_stats` | Stats on recall query performance |
| `memory_record_ctr_feedback` | Record a click-through event for a search result |
| `memory_reinforce` | Provide positive/negative feedback on memories |
| `memory_resolve_contradiction` | Resolve a specific contradiction between facts |
| `memory_resolve_thread` | Resolve or close a session thread |
| `memory_restore` | Restore a soft-deleted memory |
| `memory_retention_stats` | Stats on the adaptive-retention system |
| `memory_review_schedule` | Get SM-2 spaced-repetition review queue |
| `memory_rewrite_links` | Fix broken wiki-links after a category move |
| `memory_run_tier_migration` | Run the hot/warm/cold tier migration pass |
| `memory_scan_injection` | Run the prompt-injection detector on arbitrary content |
| `memory_sdk_demo` | Run a self-test of the Python SDK surface |
| `memory_semantic_search` | Semantic-only search using vector embeddings |
| `memory_session_admin_stats` | Administrative stats on sessions |
| `memory_shared_import` | Import a memory from the shared pool |
| `memory_shared_list` | List memories in the shared pool |
| `memory_shared_stats` | Stats on the shared pool |
| `memory_strip_provenance` | Remove agent_id/peer metadata from a memory |
| `memory_summarize` | Manually trigger summarization on a note |
| `memory_summarization_stats` | Stats on the summarization system |
| `memory_supersede` | Mark a memory as superseded by another |
| `memory_temporal_contradictions` | Query temporal contradiction log |
| `memory_temporal_query` | Query facts as of a historical point in time |
| `memory_thread_context` | Get context for a specific session thread |
| `memory_tier_stats` | Tier distribution and importance statistics |
| `memory_trash` | List soft-deleted memories pending restore/purge |
| `memory_user_profile` | Read or update the per-user preference profile |

---

## Error Handling

All tools return errors in a consistent format:

```json
{
    "error": "Error type",
    "message": "Human-readable description",
    "details": {}
}
```

Common errors:
- `MemoryNotFound` — Memory ID doesn't exist
- `InvalidCategory` — Category not in allowed list
- `DatabaseLocked` — Another process is writing
- `IndexCorrupted` — Run `agentic-memory-rebuild` to fix

## Troubleshooting

### Server not starting

```bash
# Test the server manually
agentic-memory-server

# Check for import errors
python -c "from agentic_memory import memory_mcp; print('OK')"
```

### No results from search

1. Ensure memories are saved first
2. Rebuild the index: `agentic-memory-rebuild`
3. Check if FTS5 is working: `agentic-memory-integrity`

## Further Reading

- [Python SDK](../api/python-sdk.md) — Direct Python function calls
- [REST API](../api/rest-api.md) — HTTP endpoints
- [Search Pipeline](../concepts/search-pipeline.md) — How search works
