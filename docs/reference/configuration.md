# Configuration Reference

Agentic Memory is configured via environment variables or `memory.toml`.

## Environment Variables

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_DB_PATH` | `./memory.db` | Override database path |
| `MEMORY_LOCAL_DIR` | `./memory` | Override local memory directory |
| `MEMORY_ACTIVE_DB` | Auto-detected | Force specific database path |
| `MEMORY_CONFIG_PATH` | `memory.toml` | Override config file path |
| `MEMORY_AGENT_ID` | hostname | CRDT agent identifier for multi-agent sync |
| `MEMORY_WAL_CHECKPOINT_STARTUP` | `true` | Run WAL checkpoint on startup |
| `MEMORY_WAL_CHECKPOINT_INTERVAL_S` | `300` | Periodic debounced WAL checkpoint interval in `background_worker` (S4.5, 2026-06-23). 0 to disable. |
| `MEMORY_SQLITE_MMAP_SIZE` | `268435456` | SQLite mmap_size in BYTES (S4.1, 2026-06-23). Default 256 MiB. Set to `0` to disable. See [mmap tradeoffs](#mmap-tradeoffs). |

### Features

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_MULTI_AGENT` | `true` | Enable cross-agent memory sharing |
| `MEMORY_CRDT_ENABLED` | `true` | Enable CRDT version vector tracking on every save |
| `MEMORY_SUMMARIZATION` | `true` | Auto-summarize long notes |
| `MEMORY_USER_PROFILE` | `true` | Personalize recall ranking from access history |
| `MEMORY_SELF_DIRECTED` | `true` | Self-healing: heartbeat, tier assignment, archive |
| `MEMORY_ADAPTIVE_RETENTION` | `true` | Psi formula + spaced repetition |
| `MEMORY_CONSOLIDATION` | `true` | Dedup, contradiction scan |
| `MEMORY_QUALITY_GATES` | `true` | Filter results below relevance threshold |
| `MEMORY_LLM_EXTRACTION` | `true` | Use local LLM for fact/entity extraction |
| `MEMORY_SAGA_ENABLED` | `true` | Transactional save (DB + vec + file) |
| `MEMORY_TEMPORAL_TIERS` | `true` | Hot/warm/cold tier system |
| `MEMORY_CONTEXTUAL_RETRIEVAL` | `true` | Prepend context to embeddings |

### Search

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_TEMPORAL_HALF_LIFE` | `180.0` | Temporal decay half-life in seconds |
| `MEMORY_TEMPORAL_DECAY_MODE` | `exponential` | Decay curve shape |
| `MEMORY_LATE_INTERACTION` | `true` | Enable late interaction scoring |
| `MEMORY_KNOWLEDGE_GRAPH` | `true` | Enable knowledge graph |
| `MEMORY_GRAPH_RAG_HOPS` | `3` | Max graph traversal hops |
| `MEMORY_GRAPH_RAG_EXPANSIONS` | `5` | Max graph expansion per hop |
| `MEMORY_QUERY_CACHE` | `true` | Enable search result caching |
| `MEMORY_RERANKER_DISABLED` | `false` | Disable reranker (Qwen3-0.6B) |
| `MEMORY_FORGETTING_CURVE` | `true` | Enable Ebbinghaus decay |
| `MEMORY_FORGETTING_CURVE_HALF_LIFE` | `30.0` | Forgetting curve half-life in days |
| `MEMORY_VEC_REBUILD_THRESHOLD` | `5` | Max vec/emb drift before auto-rebuild |

### Cache

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_FTS5_CACHE` | `true` | LRU result cache for search |
| `MEMORY_FTS5_CACHE_TTL` | `30` | Cache TTL in seconds |

### Multi-Agent Sync

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_SYNC_ENABLE_SERVER` | `false` | Start HTTP sync server |
| `MEMORY_SYNC_LISTEN_HOST` | `127.0.0.1` | Sync server bind address |
| `MEMORY_SYNC_LISTEN_PORT` | `9877` | Sync server port |
| `MEMORY_SYNC_INTERVAL_MINUTES` | `5` | Sync cycle interval |
| `MEMORY_SHARED_POOL_TTL_DAYS` | `30` | Shared memory pool TTL |
| `MEMORY_LLM_EXTRACTION_MODEL_ID` | `Qwen/Qwen2.5-1.5B-Instruct` | Model for LLM extraction |

### Safety

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_UNINDEXED_SAFETY_NET_LIMIT` | `1000` | Max unindexed results before fallback |

> **Note:** The repo contains 4 env vars in its source that are
> NOT documented here because they are not actually checked anywhere:
> `MEMORY_INJECTION_DETECTION` (injection scan is always on),
> `MEMORY_LOG_LEVEL` (use the stdlib `logging` module's level config),
> `MEMORY_TEST_MODE` (no such mode exists), `MEMORY_AUDIT_LOG`
> (audit is always on). They've been removed from this table to
> avoid confusing operators.

## memory.toml

Located at `~/.config/agentic-memory/memory.toml`:

```toml
[general]
db_path = "memory/memory.db"          # MEMORY_DB_PATH
wal_checkpoint_startup = true          # MEMORY_WAL_CHECKPOINT_STARTUP
unindexed_safety_net_limit = 1000     # MEMORY_UNINDEXED_SAFETY_NET_LIMIT
agent_id = ""                          # MEMORY_AGENT_ID

[search]
temporal_half_life = 180
temporal_decay_mode = "exponential"
late_interaction = true
knowledge_graph = true
graph_rag_hops = 3
graph_rag_expansions = 5
query_cache = true
reranker_disabled = false
contextual_retrieval = true
contextual_enrichment = true
forgetting_curve = true
forgetting_curve_half_life = 30
vec_rebuild_threshold = 5

[features]
multi_agent = true
summarization = true
user_profile = true
self_directed = true
adaptive_retention = true
consolidation = true
quality_gates = true
saga_enabled = true
temporal_tiers = true
crdt_enabled = true
llm_extraction = true

[cache]
fts5_cache = true
fts5_cache_ttl = 30

[multi_agent]
shared_pool_ttl_days = 30

[llm_extraction]
model_id = "Qwen/Qwen2.5-1.5B-Instruct"

[sync]
enable_server = false
listen_host = "127.0.0.1"
listen_port = 9877

[[sync.peers]]
# name = "peer-1"
# url = "http://127.0.0.1:9877"
# agent_id = "peer-1"

[sync.schedule]
interval_minutes = 5
```

## Priority Order

1. Environment variables (highest priority)
2. `memory.toml` file
3. Default values (lowest priority)

## Configuration Examples

### Minimal Setup

```bash
# Just FTS5 search, no extras
export MEMORY_DB_PATH=/data/memory.db
```

### Full-Featured Setup

```bash
# Everything enabled
export MEMORY_MULTI_AGENT=1
export MEMORY_CRDT_ENABLED=1
export MEMORY_SYNC_ENABLE_SERVER=1
export MEMORY_SYNC_LISTEN_PORT=9877
```

### Multi-Agent Peer Sync

```bash
export MEMORY_DB_PATH=/data/agent-a/memory.db
export MEMORY_AGENT_ID=agent-a
export MEMORY_SYNC_ENABLE_SERVER=1
export MEMORY_SYNC_LISTEN_PORT=9877
export MEMORY_MULTI_AGENT=1
```

## Further Reading

- [Multi-Agent Sync](../concepts/multi-agent-sync.md) — CRDT sync architecture
- [Self-Hosting](../self-hosting.md) — Production deployment guide
- [Background Tasks](../concepts/background-tasks.md) — Task queue configuration

## mmap tradeoffs

**S4.10 (2026-06-23):** the `mmap_size` PRAGMA tells SQLite to memory-map the DB file directly into the process address space, bypassing the `read()` syscall on the hot path. Default is 256 MiB.

**Pros** (measured 2026-06-23 on a 200K-row test DB with cold cache):
- ~10–14% read speedup vs no-mmap (1.70 μs → 1.70 μs warm, 2.81 μs → 2.47 μs cold).
- Eliminates one copy on the read path (kernel → user).
- Pages are demand-loaded — unused regions of the DB don't consume RAM.
- Survives `process restart` (no re-read of cached pages).

**Cons**:
- A `SIGBUS` on the mmap'd file (e.g. an external `truncate` while the DB is open) will crash the process. SQLite normally holds a `fcntl` lock that prevents this; if you run `sqlite3` against the same DB while the agent is running, you can hit it.
- Reading from mmap on macOS sometimes touches the unified-memory allocator. Not a problem in practice (256 MiB is rounding error on a 16 GiB system), but on a 4 GiB laptop the kernel may evict other pages.
- The mmap doesn't include the WAL — only the main DB file. So write-heavy workloads see no benefit.

**When to disable** (`MEMORY_SQLITE_MMAP_SIZE=0`):
- DB > 50% of available RAM.
- You're running multiple agents against the same DB and want to avoid address-space duplication.
- You're debugging a `SIGBUS` and want to rule out mmap.

**When to keep at default** (256 MiB):
- Single-agent, DB < 1 GiB, plenty of RAM. This is the common case.
