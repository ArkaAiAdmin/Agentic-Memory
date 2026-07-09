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

### `memory_semantic_search`

Semantic-only search using vector embeddings. Useful when keyword
search returns nothing and you know the concept but not the words.

```python
memory_semantic_search(query="how to handle race conditions", limit=5)
```

---

### `memory_facts_search`

Search the knowledge-graph facts table (subject-predicate-object triples).

```python
memory_facts_search(subject="SQLite", predicate="uses", limit=10)
```

---

### `memory_graph_search`

Walk the knowledge graph from a starting entity, returning
neighbors up to N hops.

```python
memory_graph_search(start="agentic-memory", max_hops=2, limit=20)
```

---

### `memory_session_start`

Boot a session: returns recent memories, profile state, and any
active context. Designed to be called by hooks on session start.

```python
memory_session_start(project_id="my-app")
```

---

### `memory_user_profile`

Read or update the per-user preference profile.

```python
memory_user_profile(operation="get")
memory_user_profile(operation="set", profile={...})
```

---

### `memory_delete`

Soft-delete a memory. Can be restored within 30 days.

```python
memory_delete(id="lessons/sqlite-wal-mode", hard=False)
```

**Parameters:**
- `id` (str, required) — Memory ID
- `hard` (bool, optional) — Immediately purge (default: false, soft-delete)

---

### `memory_restore`

Restore a soft-deleted memory.

```python
memory_restore(id="lessons/sqlite-wal-mode")
```

---

### `memory_check_contradictions`

Scan the most-recent memories for facts that conflict with existing
ones. Surfaces a structured warning and writes a
`memory_save_contradiction_check` audit row.

```python
memory_check_contradictions(top_n=20, min_confidence="low")
```

---

### `memory_scan_injection`

Run the prompt-injection detector on arbitrary content. Returns a
risk score and a list of matched patterns.

```python
memory_scan_injection(content="...")
```

---

### `memory_rebuild`

Rebuild the search index from markdown files. Use after a schema
migration or to repair index drift.

```python
memory_rebuild(scope="active")
```

**Parameters:**
- `scope` (str, optional) — One of: active (default), local, global

---

### `memory_supersede`

Mark a memory as superseded by another. Sets `valid_to` on the
old note and `superseded_by` on the new one for temporal
filtering.

```python
memory_supersede(old_id="lessons/v1", new_id="lessons/v2")
```

---

### `memory_profile_access`

Record a profile-relevant access event. Used by the auto-profiling
loop; rarely called directly by agents.

```python
memory_profile_access(note_id="lessons/sqlite-wal-mode", source="search")
```

---

## Admin Tools (87)

All admin operations go through the `memory_maintenance` grouped
tool, dispatched by `operation=`. The full list (single source of
truth: `tool_registry.ADMIN_TOOLS`):

| Operation | Purpose |
|-----------|---------|
| `memory_maintenance` | The dispatcher itself — pass `operation=...` to call any admin op |
| `memory_adaptive_retention` | Compute the psi-formula half-life for a memory |
| `memory_arc_stats` | Read ARC eviction-cache stats without recomputing |
| `memory_arc_reset` | Reset ARC ghost lists and stats (operator escape hatch) |
| `memory_audit` | Health check on the memory database |
| `memory_audit_query` | Query the per-call audit log with filters |
| `memory_auto_save_hook` | Programmatic equivalent of the opencode tool-complete hook |
| `memory_auto_save_status` | Show auto-save health, last batch, daemon PID |
| `memory_auto_summarize` | Trigger TF-IDF summarization on a note |
| `memory_auto_share` | Auto-publish opt-in memories to the shared pool |
| `memory_backfill_all` | Run the audit pipeline (FTS, vec, KG, etc.) |
| `memory_check_concept_drift` | Compute centroid-vs-centroid drift for an embedding |
| `memory_check_integrity` | Full DB integrity check |
| `memory_check_embedding_model` | Verify active embedding model revision |
| `memory_compact` | Run deduplication and consolidation |
| `memory_consolidate` | Run fact consolidation + contradiction detection |
| `memory_compile_skill` | Compile a lesson into an executable agent skill |
| `memory_crdt_status` | Show CRDT version-vector state for the local agent |
| `memory_crdt_sync` | Push/pull CRDT state with a peer |
| `memory_daily_digest` | Roll auto-saves into a daily summary note |
| `memory_dashboard` | Return a high-level summary (counts, health) |
| `memory_detect_contradictions` | Force a contradiction scan over a category |
| `memory_duplicates` | Find near-duplicate memories in the corpus |
| `memory_extract_skills` | Refresh the skills cache from existing lessons |
| `memory_facts_list` | List extracted SPO triples with filters |
| `memory_facts_stats` | Statistics on the facts table |
| `memory_graph_stats` | Statistics on the KG (entities, edges, density) |
| `memory_heartbeat` | Run the periodic self-healing + tier sweep |
| `memory_incremental_update` | Re-index a single memory (FTS, vec, chunk, KG) |
| `memory_ingest_file` | Read a file and save its contents as a memory |
| `memory_ingest_url` | Fetch a URL and save its content as a memory |
| `memory_list_drift_alarms` | List per-memory concept-drift alarms (v15) |
| `memory_list_skills` | List cached skills ordered by hit count |
| `memory_llm_unload` | Force-unload the LLM from memory to release GPU |
| `memory_merge_embeddings` | Merge duplicate embedding rows after consolidation |
| `memory_merge_suggestions` | List candidate memory merges from the dedup pipeline |
| `memory_metrics_server` | Start the Prometheus-format metrics endpoint |
| `memory_okf_export` | Export memories to OKF (one .md per memory) |
| `memory_okf_import` | Import memories from an OKF directory |
| `memory_pinned_decay_check` | Find pinned notes that haven't been accessed in N days |
| `memory_profile_stats` | Read user-profile hit counts and top categories |
| `memory_purge_auto_saves` | Hard-delete auto-saves older than N days |
| `memory_purge_expired` | Hard-delete tombstoned notes older than 30 days |
| `memory_quality_filter` | Apply the quality_gates filter to a result set |
| `memory_quality_stats` | Show quality-gate pass/fail rates |
| `memory_record_ctr_feedback` | Record a click-through event for a search result |
| `memory_reinforce` | Provide positive/negative feedback on memories |
| `memory_retention_stats` | Stats on the adaptive-retention system |
| `memory_review_schedule` | Get SM-2 spaced-repetition review queue |
| `memory_rewrite_links` | Fix broken wiki-links after a category move |
| `memory_run_tier_migration` | Run the hot/warm/cold tier migration pass |
| `memory_share` | Publish a memory to the shared pool |
| `memory_shared_import` | Import a memory from the shared pool |
| `memory_shared_list` | List memories in the shared pool |
| `memory_shared_stats` | Stats on the shared pool |
| `memory_strip_provenance` | Remove agent_id/peer metadata from a memory |
| `memory_summarize` | Manually trigger summarization on a note |
| `memory_summarization_stats` | Stats on the summarization system |
| `memory_tier_stats` | Tier distribution and importance statistics |
| `memory_trash` | List soft-deleted memories pending restore/purge |
| `memory_agent_init` | Initialize a CRDT agent identity for the local host |
| `memory_agent_clear` | Clear the local CRDT agent identity |
| `memory_agent_list` | List known CRDT agent identities |
| `memory_sdk_demo` | Run a self-test of the Python SDK surface |

The 9 admin tools added on 2026-06-22:
- `memory_list_drift_alarms` (drift alarms UI, v15)
- `memory_arc_reset` (operator escape hatch for ARC state)
- `memory_extract_skills` (refresh `memory_skills` cache)
- `memory_list_skills` (list cached skills)
- `memory_auto_share` (auto-publish to shared pool)
- `memory_agent_init` / `memory_agent_clear` / `memory_agent_list` (CRDT agent identity)
- `memory_sdk_demo` (Python SDK quickstart)

For historical reference, the full tool-addition timeline (per
`tool_registry.py`):

- **2026-06-15** (1 tool): `memory_record_ctr_feedback`
- **2026-06-17** (4 tools): `memory_crdt_sync`, `memory_crdt_status`,
  `memory_okf_export`, `memory_okf_import`
- **2026-06-18** (13 tools, H4 fix): `memory_heartbeat`,
  `memory_tier_stats`, `memory_run_tier_migration`,
  `memory_check_embedding_model`, `memory_incremental_update`,
  `memory_merge_embeddings`, `memory_duplicates`,
  `memory_merge_suggestions`, `memory_llm_unload`,
  `memory_ingest_file`, `memory_ingest_url`, `memory_dashboard`,
  `memory_metrics_server`
- **2026-06-22** (9 tools): see list above

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
