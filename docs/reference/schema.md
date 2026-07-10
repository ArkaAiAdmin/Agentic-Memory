# Database Schema

Agentic Memory uses SQLite with FTS5 for full-text search. Schema version **37** (defined in `migration_runner.py`; 38 migrations, ~49 user-visible tables; ~62 total including FTS5 virtual tables).

Migration history (most recent first):
- v21: `kg_entity_crdt` + `kg_edge_crdt` tables for CRDT multi-agent merge support. Enables conflict-free entity/edge sync across peers.
- v20: `kg_facts_fts` FTS5 virtual table + 3 sync triggers (ai, ad, au). Brings kg_facts in line with the other 3 text-searchable tables (memories, memory_chunks, kg_entities) which all have FTS5. The new FTS table is contentless (backed by kg_facts) — no storage duplication.
- v19: `kg_facts.subject_entity_id` and `object_entity_id` FKs now have `ON DELETE SET NULL`. Pre-existing bug fix: `kg_dedup.merge_entities()` was failing with "FOREIGN KEY constraint failed" when a fact referenced the merged entity. Fixes a bug that had been failing the background worker every 5 minutes.
- v18: Fact-level temporal KG (T1 of the temporal-kg plan). Adds 9 columns to `kg_facts` (event_time, event_time_granularity, transaction_time, valid_at, invalid_at, superseded_by, supersedes, contradiction_score, invalidation_reason) + 3 indexes. Enables bi-temporal validity and time-travel queries. See [Temporal KG concept doc](../concepts/temporal-kg.md).
- v17: `kg_edges.kg_entities` and `backlinks.memories` FK constraints added (B-3 fix). `kg_edges` uses `ON DELETE SET NULL` (entities are shared across notes); `backlinks` uses `ON DELETE CASCADE`. `kg_entities` is left without a FK (shared); orphans cleaned by `memory_integrity.repair_kg_orphans`.
- v16: `concept_drift` table moved to canonical SQL migration (was previously Python-only)
- v15: `drift_alarms` table + `memory_embeddings.ssm_state` column
- v14: `arc_ghosts` + `arc_stats` tables
- v13: `memory_field_crdt` table for per-field LWWES

All schema is defined in `migrations/` files and applied by `migration_runner.py` at startup.

## Core Tables

### `memories`

Memory metadata and content. Current schema (migrations 001-008):

```sql
CREATE TABLE memories (
    id                 TEXT PRIMARY KEY,
    content            TEXT NOT NULL,
    source_file        TEXT NOT NULL,
    tags               TEXT DEFAULT '[]',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    observed_at        TEXT NOT NULL,
    pinned             INTEGER DEFAULT 0,
    importance         INTEGER DEFAULT 3,
    decay              TEXT DEFAULT 'none',
    score              REAL DEFAULT 1.0,
    supersedes         TEXT,
    repo_id            TEXT,
    access_count       INTEGER DEFAULT 1,
    success_score      REAL DEFAULT 0.0,
    fitness_score      REAL DEFAULT 1.0,
    conflict_policy    TEXT DEFAULT 'supersede',
    version_vector     TEXT DEFAULT '{}',
    logical_clock      INTEGER DEFAULT 0,
    consolidation_state TEXT DEFAULT 'working',
    valid_from         TEXT,
    valid_to           TEXT,
    superseded_by      TEXT,
    last_accessed      TEXT,
    deleted_at         TEXT,
    deleted_by         TEXT,
    context_prefix     TEXT,
    category           TEXT,
    tier               TEXT,
    importance_score   REAL,
    metadata           TEXT
);
```

Key columns:
- `version_vector` — JSON dict for CRDT multi-agent sync (e.g., `{"agent-a": 5, "agent-b": 3}`)
- `logical_clock` — Local agent's clock for CRDT conflict resolution
- `conflict_policy` — CRDT merge strategy (`supersede`, `replace`, `coexist`)
- `supersedes` — Tracks which note this note replaced (CRDT)
- `valid_from` / `valid_to` / `superseded_by` — Temporal versioning
- `consolidation_state` — Fact dedup pipeline state
- `fitness_score` — Search ranking score
- `metadata` — Arbitrary JSON metadata

### `memories_fts`

FTS5 virtual table for full-text search:

```sql
CREATE VIRTUAL TABLE memories_fts USING fts5(
    content,
    tags,
    tokenize='porter unicode61'
);
```

Kept in sync via triggers `memories_ai`, `memories_ad`, `memories_au`.

### `memory_embeddings`

Vector embeddings for semantic search:

```sql
CREATE TABLE memory_embeddings (
    memory_id      TEXT PRIMARY KEY,
    content_hash   TEXT NOT NULL,
    embedding      BLOB NOT NULL,
    model_revision TEXT NOT NULL,
    dim            INTEGER NOT NULL,
    updated_at     REAL NOT NULL,
    ssm_state      TEXT,                       -- v15: streaming / partial-embedding state
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
);
```

### `memory_vec_idx` / `memory_vec_keys`

usearch HNSW vector index:

```sql
CREATE TABLE memory_vec_idx (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    n_vectors        INTEGER NOT NULL,
    dim              INTEGER NOT NULL,
    metric           TEXT NOT NULL,
    quantization     TEXT NOT NULL,
    connectivity     INTEGER NOT NULL,
    expansion_add    INTEGER NOT NULL,
    expansion_search INTEGER NOT NULL,
    built_at         REAL NOT NULL,
    index_blob       BLOB NOT NULL,
    key_count        INTEGER NOT NULL
);

CREATE TABLE memory_vec_keys (
    key       INTEGER PRIMARY KEY,
    memory_id TEXT NOT NULL UNIQUE REFERENCES memories(id) ON DELETE CASCADE
);
```

### `backlinks`

Bidirectional wiki-link references:

```sql
CREATE TABLE backlinks (
    source_id TEXT,
    target_id TEXT,
    PRIMARY KEY (source_id, target_id)
);
```

### `memory_chunks` / `memory_chunks_fts`

Long-note chunking for QW5 indexing:

```sql
CREATE TABLE memory_chunks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id    TEXT NOT NULL,
    chunk_idx    INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset   INTEGER NOT NULL,
    content      TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(parent_id, chunk_idx),
    FOREIGN KEY (parent_id) REFERENCES memories(id) ON DELETE CASCADE
);

CREATE VIRTUAL TABLE memory_chunks_fts USING fts5(
    content, parent_id, chunk_idx,
    content=memory_chunks,
    content_rowid=id,
    tokenize='porter unicode61'
);
```

### `file_mtimes`

Tracks file modification times for incremental index updates:

```sql
CREATE TABLE file_mtimes (
    path         TEXT PRIMARY KEY,
    mtime        REAL NOT NULL,
    content_hash TEXT NOT NULL
);
```

## Multi-Agent Tables

### `shared_memories`

Cross-agent shared memory pool:

```sql
CREATE TABLE shared_memories (
    id             TEXT PRIMARY KEY,
    agent_id       TEXT NOT NULL,
    content        TEXT NOT NULL,
    category       TEXT,
    tags           TEXT,
    shared_at      REAL NOT NULL,
    source_note_id TEXT,
    metadata       TEXT
);
```

Used by `multi_agent.py` for the shared pool feature (`MEMORY_MULTI_AGENT=1`).

### `sync_log`

CRDT peer sync audit trail (migration 008):

```sql
CREATE TABLE sync_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_name      TEXT NOT NULL,
    peer_url       TEXT NOT NULL,
    peer_agent_id  TEXT NOT NULL,
    direction      TEXT NOT NULL CHECK (direction IN ('push', 'pull', 'sync')),
    started_at     REAL NOT NULL,
    completed_at   REAL,
    success        INTEGER DEFAULT 0,
    changes_pushed INTEGER DEFAULT 0,
    changes_pulled INTEGER DEFAULT 0,
    error_message  TEXT,
    error_count    INTEGER DEFAULT 0,
    duration_ms    INTEGER DEFAULT 0
);
```

## Knowledge Graph Tables

### `kg_entities`

```sql
CREATE TABLE kg_entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    entity_type TEXT,
    mentions    INTEGER DEFAULT 1,
    created_at  TEXT,
    updated_at  TEXT,
    UNIQUE(name, entity_type)
);
```

### `kg_edges`

```sql
CREATE TABLE kg_edges (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
    relation  TEXT NOT NULL DEFAULT 'related_to',
    weight    REAL DEFAULT 1.0,
    created_at TEXT,
    valid_at  TEXT,
    invalid_at TEXT,
    UNIQUE(source_id, target_id, relation)
);
```

### `kg_facts`

Extracted SPO triples. Current schema includes v18 temporal columns,
v19 entity FKs (ON DELETE SET NULL), v20 FTS5 triggers, and v21 kg_crdt
tables (`kg_entity_crdt`, `kg_edge_crdt`) for CRDT merge support.

```sql
CREATE TABLE kg_facts (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    subject                TEXT NOT NULL,
    predicate              TEXT NOT NULL,
    object                 TEXT NOT NULL,
    confidence             REAL DEFAULT 1.0,
    locked                 INTEGER DEFAULT 0,
    first_seen             REAL,
    last_seen              REAL,
    mention_count          INTEGER DEFAULT 1,
    source_memory          TEXT,
    context                TEXT,
    -- v18: temporal KG columns
    subject_entity_id      INTEGER REFERENCES kg_entities(id) ON DELETE SET NULL,  -- v19
    object_entity_id       INTEGER REFERENCES kg_entities(id) ON DELETE SET NULL,  -- v19
    event_time             REAL,
    event_time_granularity TEXT,
    transaction_time       REAL,
    valid_at               REAL,
    invalid_at             REAL,
    superseded_by          INTEGER REFERENCES kg_facts(id) ON DELETE SET NULL,
    supersedes             INTEGER REFERENCES kg_facts(id) ON DELETE SET NULL,
    contradiction_score    REAL DEFAULT 0.0,
    invalidation_reason    TEXT,
    UNIQUE(subject, predicate, object),
    FOREIGN KEY (source_memory) REFERENCES memories(id) ON DELETE SET NULL
);
```

3 indexes added in v18 for time-aware queries:
- `idx_kg_facts_validity` (valid_at, invalid_at)
- `idx_kg_facts_superseded_by`
- `idx_kg_facts_event_time`

### `kg_facts_fts` (v20) and KG CRDT (v21)

FTS5 virtual table for ranked fact search. Contentless (backed by
`kg_facts`); 3 sync triggers keep it current. v21 adds `kg_entity_crdt`
and `kg_edge_crdt` tables for CRDT-based conflict-free entity/edge
merge across multi-agent peers.

```sql
CREATE VIRTUAL TABLE kg_facts_fts USING fts5(
    subject, predicate, object, context,
    content='kg_facts', content_rowid='id',
    tokenize='porter unicode61'
);
```

Sync triggers: `kg_facts_fts_ai` (after insert), `kg_facts_fts_ad` (after
delete), `kg_facts_fts_au` (after update). Use the FTS5 MATCH syntax
for ranked search (O(log n) vs O(n) for `LIKE %query%`).

## Audit & Observability Tables

### `memory_audit_log`

Append-only MCP tool audit log:

```sql
CREATE TABLE memory_audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    tool          TEXT NOT NULL,
    args          TEXT,
    results_count INTEGER,
    top1_id       TEXT,
    latency_ms    REAL NOT NULL,
    error         TEXT,
    request_id    TEXT
);
```

### `memory_ctr_feedback`

Click-through rate feedback for search ranking:

```sql
CREATE TABLE memory_ctr_feedback (
    id             TEXT PRIMARY KEY,
    query_id       TEXT NOT NULL,
    returned_at    REAL NOT NULL,
    clicked_at     REAL,
    dismissed_at   REAL,
    source         TEXT,
    ranking_params TEXT
);
```

### `concept_drift`

Concept drift detection for search quality. Stores a centroid-vs-centroid
distance event each time `memory_check_concept_drift` detects drift above
the configured threshold. The `drifted_dimensions` column stores the
current centroid (JSON-encoded numpy array) so the next run can compute
drift against it.

```sql
CREATE TABLE concept_drift (
    id                TEXT PRIMARY KEY,
    drift_metric      REAL NOT NULL,
    drifted_dimensions TEXT,
    triggered_at      REAL NOT NULL,
    acknowledged      INTEGER DEFAULT 0
);
```

### `drift_alarms`

Per-memory concept-drift alarms (added in v15). One row per memory
that contributed to a drift event. Severity-tiered (info/warning/
critical) and supports acknowledgement workflow via
`memory_list_drift_alarms`.

```sql
CREATE TABLE drift_alarms (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id       TEXT    NOT NULL,
    concept         TEXT    NOT NULL,
    drift_score     REAL    NOT NULL,
    threshold       REAL    NOT NULL,
    alarm_level     TEXT    NOT NULL CHECK(alarm_level IN ('info', 'warning', 'critical')),
    detected_at     TEXT    NOT NULL,
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    notes           TEXT,
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
);
```

## Operational Tables

### `task_queue`

Async background task queue:

```sql
CREATE TABLE task_queue (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type        TEXT NOT NULL,
    payload          TEXT,
    status           TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    priority         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    started_at       TEXT,
    completed_at     TEXT,
    error            TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 3,
    source_note_id   TEXT
);
```

### `arc_ghosts` (v14)

Adaptive Replacement Cache ghost lists. One row per memory that
was evicted. Acts as the B1/B2 ghost lists in the ARC algorithm;
`would_have_been_hit` flips to 1 the next time the memory is
needed but no longer in cache.

```sql
CREATE TABLE arc_ghosts (
    memory_id           TEXT PRIMARY KEY,
    evicted_at          TEXT NOT NULL,
    tier                TEXT NOT NULL,
    would_have_been_hit INTEGER DEFAULT 0
);

CREATE INDEX idx_arc_ghosts_evicted_at ON arc_ghosts(evicted_at);
```

### `arc_stats` (v14)

Adaptive Replacement Cache stats key/value table. The
`memory_arc_stats` MCP tool reads state without recomputing.

```sql
CREATE TABLE arc_stats (
    key   TEXT PRIMARY KEY,
    value REAL DEFAULT 0.0
);
```

### `memory_skills`

Procedural knowledge cache (migration 007):

```sql
CREATE TABLE memory_skills (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL UNIQUE,
    source_memory_id TEXT,
    topic            TEXT,
    description      TEXT,
    triggers         TEXT DEFAULT '[]',
    steps            TEXT DEFAULT '[]',
    content_hash     TEXT,
    hit_count        INTEGER DEFAULT 0,
    last_used_at     REAL,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);
```

### `user_access_log` / `user_profile_access_log`

Access tracking for user profiling:

```sql
CREATE TABLE user_access_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id   TEXT NOT NULL,
    access_ts REAL NOT NULL,
    source    TEXT NOT NULL DEFAULT 'unknown',
    FOREIGN KEY (note_id) REFERENCES memories(id) ON DELETE CASCADE
);

CREATE TABLE user_profile_access_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id     TEXT NOT NULL,
    source      TEXT DEFAULT 'search',
    category    TEXT,
    tags        TEXT,
    accessed_at REAL NOT NULL,
    FOREIGN KEY (note_id) REFERENCES memories(id) ON DELETE CASCADE
);
```

### `review_schedule`

Spaced repetition schedule:

```sql
CREATE TABLE review_schedule (
    memory_id        TEXT PRIMARY KEY,
    retrieval_count  INTEGER DEFAULT 0,
    interval_days    REAL DEFAULT 1.0,
    next_review      TEXT NOT NULL,
    last_reviewed    TEXT,
    ease_factor      REAL DEFAULT 2.5
);
```

### `schema_version`

Migration tracking:

```sql
CREATE TABLE schema_version (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
```

## Migration History

| # | File | Description |
|---|------|-------------|
| 1 | `001_schema_version.sql` | Baseline: schema version tracking |
| 2 | `002_memory_embeddings.sql` | Memory embedding cache table |
| 3 | `003_memory_audit_log.sql` | Append-only MCP audit log |
| 4 | `004_memory_vec_idx.sql` | Vector index tables (usearch HNSW) |
| 5 | `005_columns_indexes_chunks.sql` | New columns, indexes, memory_chunks, FTS5 porter |
| 6 | `006_check_constraints_and_indexes.sql` | CHECK constraints, partial indexes |
| 7 | `007_memory_skills.sql` | memory_skills table |
| 8 | `008_sync_log.sql` | sync_log table for multi-agent sync tracking |
| 9 | `009_kg_facts_entity_fks.sql` | Foreign keys on kg_facts → kg_entities |
| 10 | `010_memory_embeddings_memory_id_idx.sql` | Index on memory_embeddings.memory_id |
| 11 | `011_idx_memories_observed_at.sql` | Index on memories.observed_at for temporal queries |
| 12 | `012_kg_extraction_stats.sql` | kg_extraction_stats table for extraction quality metrics |
| 13 | `013_field_level_crdt.sql` | memory_field_crdt table for per-field LWWES (v13) |
| 14 | `014_arc_cache.sql` | arc_ghosts + arc_stats tables for ARC eviction (v14) |
| 15 | `015_drift_alarms.sql` | drift_alarms table for per-memory concept-drift alarms (v15) + memory_embeddings.ssm_state column |
| 16 | `016_concept_drift.sql` | concept_drift table moved to canonical SQL (was previously created in Python) |
| 17 | `017_kg_cascade.sql` | kg_edges and backlinks get FK constraints (ON DELETE SET NULL/CASCADE). Closes B-3 audit gap where saga rollbacks left orphans. |
| 18 | `018_fact_temporal.sql` | 9 columns + 3 indexes on kg_facts for bi-temporal validity (event_time, valid_at, invalid_at, superseded_by, etc.). See [Temporal KG concept doc](../concepts/temporal-kg.md). |
| 19 | `019_kg_facts_entity_fk.sql` | kg_facts.subject/object_entity_id FKs get ON DELETE SET NULL. Pre-existing bug fix for entity dedup. |
| 20 | `020_kg_facts_fts.sql` | kg_facts FTS5 virtual table + 3 sync triggers (ai, ad, au). Brings kg_facts in line with the other 3 text-searchable tables. |
| 21 | `021_kg_crdt.sql` | kg_entity_crdt + kg_edge_crdt tables for CRDT multi-agent merge support. Enables conflict-free entity/edge sync across peers. |
| 22 | `022_session_memory.sql` | sessions, decision_threads, thread_events, session_compaction_log tables for the session memory system. |
| 23 | `023_add_audit_status.sql` | memory_audit_log.status column + status-based indexes for audit-phase triage. |
| 24 | `024_chunk_embeddings.sql` | memory_chunk_embeddings, memory_chunk_vec_idx, memory_chunk_vec_keys for chunk-level multi-vector search. |
| 25 | `025_belief_plumbing.sql` | belief_assertions table + kg_facts.fact_type column (Sprint 1 fact/belief separation). |
| 26 | `026_belief_assertions.sql` | belief_assertions tables: agent assertions, assertion history, support relations for evidential reasoning. |
| 27 | `027_revision_log.sql` | memory_revision_log table for note revision tracking + diff storage (Sprint 3). |
| 28 | `028_entailment_chains.sql` | entailment_chains table for logical inference linking between facts (Sprint 3). |
| 29 | `029_graph_snapshots.sql` | graph_snapshots table for point-in-time KG serialization (Sprint 4 graph analytics). |
| 30 | `030_community_id_and_betweenness.sql` | kg_entities.community_id + betweenness columns for graph community detection (Sprint 4). |
| 31 | `031_outbox_events.sql` | memory_events outbox table for REST/WS API event sourcing. |
| 32 | `032_outbox_events_scoped.sql` | Scoped outbox update trigger (semantic columns only) for memory_events. |
| 33 | `033_shared_skills.sql` | memory_skills.hit_vector, last_used_vector, logical_clock columns for CRDT-aware skill hit counting. |
| 34 | `034_entailment_validation.sql` | Entailment validation tables and triggers. |
| 35 | `035_shared_memories_target_agent_id.sql` | shared_memories.target_agent_id + shared_with columns for directed sharing and the shared_with_me filter. |
| 36 | `036_embedding_model_tracking.sql` | embedding model tracking in memory_vec_idx for model-version-aware vector index management. |
| 37 | `037_cron_runs.sql` | cron_runs table for consolidated cron execution tracking. Records job_name, started_at, completed_at, status, duration_ms, error, output for every cron run. Used by memory_system_health MCP tool. |
