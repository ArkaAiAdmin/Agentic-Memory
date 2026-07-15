# Environment Variables Reference

Complete list of `MEMORY_*` environment variables not covered by `memory.toml` keys.
Each entry shows the default value and where it's read in the codebase.

---

## Database & Locking

| Env Var | Default | Description |
|---------|---------|-------------|
| `MEMORY_DB_LOCK_ATTEMPTS` | `20` | Max flock acquisition attempts in `save/pipeline.py:352`. Each attempt sleeps briefly before retrying. |
| `MEMORY_REBUILD_VEC_INDEX` | _(none)_ | Path to `rebuild_vec_index.py`. Overrides auto-detection in `background/background_worker.py:306`. When unset, the worker resolves the script relative to install root. |

## Background Worker

| Env Var | Default | Description |
|---------|---------|-------------|
| `MEMORY_WORKER_BATCH_SIZE` | `20` | Max tasks processed per drain cycle (`background/background_worker.py:79`). Capped by DB pool size - 4. |
| `MEMORY_WORKER_TASK_TIMEOUT_S` | `120` | Per-task watchdog timeout in seconds (`background/background_worker.py:1223`). Tasks exceeding this are killed. |
| `MEMORY_WORKER_DRAIN_MAX_WALL_S` | `600` | Max wall-clock seconds for a single drain loop (`background/background_worker.py:1327`). Safety cap to prevent unbounded processing. |
| `MEMORY_WORKER_PROCESS_TIMEOUT_S` | `3600` | Max seconds for the entire worker process (`background/background_worker.py:1523`). Hard safety cap — worker exits after this. |
| `MEMORY_WORKER_INTERVAL` | `300` | Poll interval in seconds between drain cycles (`background/background_worker.py:1743`). Default 5 minutes. |
| `MEMORY_WORKER_MAX_TASKS` | `10000` | Cap on tasks processed per drain invocation (`background/background_worker.py:1748`). Safety belt against runaway queues. |

## Reconciler

| Env Var | Default | Description |
|---------|---------|-------------|
| `MEMORY_RECONCILER_N_WORKERS` | `1` | Number of reconciler worker processes (`background/background_worker.py:1111`). When > 1, uses a multiprocessing pool instead of a single daemon thread. |

## WAL

| Env Var | Default | Description |
|---------|---------|-------------|
| `MEMORY_WAL_CHECKPOINT_THRESHOLD_MB` | `10.0` | WAL file size in MB that triggers a checkpoint (`background/background_worker.py:1140`). Prevents WAL file from growing unbounded. |

## Contradiction Detection

| Env Var | Default | Description |
|---------|---------|-------------|
| `MEMORY_CONTRADICTION_AUTO_RESOLVE_LLM` | _(disabled)_ | When set to `"1"`, enables LLM-based contradiction resolution (`kg/contradiction_resolver.py:75`). Uses 4 strategies: supersede_b_with_a, supersede_a_with_b, merge, keep_both. |

## Search & CTR

| Env Var | Default | Description |
|---------|---------|-------------|
| `MEMORY_CHUNK_SEARCH` | `"1"` | Enable chunk-level vector search (`infra/embedding_search.py:315`). Set to `"0"`, `"false"`, or `"no"` to disable. |
| `MEMORY_CTR_CLICK_WINDOW_HOURS` | `4` | Hours to look back for CTR click events (`save/indexers.py:207`). Converts to seconds internally (default 14400s). |
| `MEMORY_CTR_EPSILON` | `0.1` | Epsilon for epsilon-greedy exploration in CTR tuning (`search/scoring.py:459`). Higher = more exploration. |
| `MEMORY_CTR_TUNING` | _(enabled)_ | Set to `"0"` to disable CTR-driven rerank weight tuning. When enabled, learns per-query-type channel weights from `memory_ctr_feedback` via Thompson sampling. Results cached for 5 minutes. |

## Write Queue

| Env Var | Default | Description |
|---------|---------|-------------|
| `MEMORY_WRITE_QUEUE_RESP_TIMEOUT_S` | `60.0` | Max seconds to wait for a write-queue response (`infra/db_write_queue.py:141`). |
| `MEMORY_WRITE_QUEUE_IDLE_S` | `30.0` | Seconds of idle before the queue worker yields its slot (`infra/db_write_queue.py:388`). Allows competing writers to proceed. |
| `MEMORY_WRITE_QUEUE_MAX_S` | `300.0` | Max seconds a single queue item can hold the writer slot (`infra/db_write_queue.py:389`). Prevents starvation. |

## Multi-Agent Sync

| Env Var | Default | Description |
|---------|---------|-------------|
| `MEMORY_SYNC_TOKEN` | _(empty)_ | Bearer auth token for the sync server (`infra/sync_server.py:53`). Required when the server is bound to a non-loopback address. |
| `MEMORY_SYNC_CORS_ORIGINS` | _(empty)_ | Comma-separated CORS allowed origins (`infra/sync_server.py:60`). Empty = no CORS headers. |
| `MEMORY_SYNC_HMAC_SECRET` | _(empty)_ | HMAC-SHA256 signing secret for sync payloads (`infra/sync_server.py:67`). Optional integrity layer. |
| `MEMORY_SYNC_MAX_AGE` | `300` | Max age in seconds for sync requests — replay protection (`infra/sync_server.py:72`). |
| `MEMORY_SYNC_MAX_BODY` | `10485760` (10 MB) | Max request body size in bytes (`infra/sync_server.py:76`). |
| `MEMORY_SYNC_PEER` | _(empty)_ | URL of the peer sync server for cron-driven sync (`infra/sync_client.py:694`). |
| `MEMORY_SYNC_PEER_NAME` | _(peer agent_id)_ | Display name for the peer. Falls back to `peer_agent_id` if unset (`infra/config.py:802`). |
| `MEMORY_SYNC_LIMIT` | `200` | Max memories per sync exchange (`cron/cron_sync.py:80`). |

## API Server

| Env Var | Default | Description |
|---------|---------|-------------|
| `MEMORY_API_CORS_ORIGINS` | _(empty)_ | Comma-separated CORS allowed origins for the REST/WS API (`infra/api_server.py:30`). |

## Authorization

| Env Var | Default | Description |
|---------|---------|-------------|
| `MEMORY_AUTH_MODE` | `"closed"` | Auth mode: `"closed"` = fail-closed (deny unauthenticated), `"open"` = pre-RBAC passthrough (`infra/authorizer.py:183`). |

## Dashboard & Metrics

| Env Var | Default | Description |
|---------|---------|-------------|
| `MEMORY_DASHBOARD_HOST` | `"127.0.0.1"` | Bind address for the dashboard server (`mcp_dashboard.py:144`). |
| `MEMORY_METRICS_TENANT` | _(empty)_ | Tenant ID for metrics scoping (`infra/metrics_server.py:73`). Empty = no tenant filtering. |

## Process Guards

| Env Var | Default | Description |
|---------|---------|-------------|
| `MEMORY_GUARD_RSS_MB` | `500` | Max allowed RSS in MB before the worker memory guard kills the process (`scripts/worker_memory_guard.py:47`). |
| `MEMORY_GUARD_FOOTPRINT_MB` | `1024` | Max allowed physical memory footprint in MB (`scripts/worker_memory_guard.py:49`). |

## LLM Extraction

| Env Var | Default | Description |
|---------|---------|-------------|
| `MEMORY_LLM_HYBRID` | _(enabled)_ | When set to `"0"`, disables LLM extraction entirely — regex-only (`fact/fact_extract.py:905`). Default behavior uses LLM for memories above the hybrid threshold. |

## Scope & Config

| Env Var | Default | Description |
|---------|---------|-------------|
| `MEMORY_SCOPE` | _(auto-detected)_ | Process execution scope: `production`, `staging`, `development`, or `test` (`infra/scope.py:44`). Drives config-drift enforcement policy. Auto-detected if unset. |
| `MEMORY_CONFIG_DIR` | `~/.config/agentic-memory` | Base directory for config file resolution (`background/inbox.py:119`, `eval/conftest.py:183`). |

## Testing

| Env Var | Default | Description |
|---------|---------|-------------|
| `MEMORY_TEST_EMBEDDING` | `"0"` | When set to `"1"`, activates the test embedding backend (intfloat/e5-small-v2, SentenceTransformer) for CI tests (`eval/conftest.py:34`). Production `[embedding]` is never modified. |
