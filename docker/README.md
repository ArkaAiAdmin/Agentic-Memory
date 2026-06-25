# Docker Compose (Phase 4.1)

This directory contains the containerized deployment for agentic-memory.
A single image runs three services, selected via the `SERVICE` env var:

| Service | Entry point            | Purpose                                   |
|---------|------------------------|-------------------------------------------|
| `mcp`   | `cli.py server`        | FastMCP server (stdio or HTTP)            |
| `sync`  | `sync_server.py`       | CRDT peer sync HTTP server (port 9877)    |
| `cron`  | `docker/cron_runner.py`| Replaces host crontab for scheduled jobs  |

## Quick start

```sh
# Build the image (one time)
docker compose build

# Start all three services
docker compose up -d

# Start only the MCP server
docker compose up -d mcp

# View logs
docker compose logs -f mcp

# Stop everything
docker compose down
```

The data directory (`/data` inside the container) is backed by the
`memory-data` named volume, so the SQLite DB and lock files persist
across `docker compose down` / `up` cycles.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    agentic-memory:latest                    │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐              │
│  │ cli.py   │  │ sync_    │  │ cron_runner  │              │
│  │ server   │  │ server   │  │ .py          │              │
│  │ (mcp)    │  │ (sync)   │  │ (cron)       │              │
│  └─────┬────┘  └────┬─────┘  └──────┬───────┘              │
│        │            │               │                       │
│        └────────────┴───────────────┘                       │
│                  │                                          │
│                  ▼                                          │
│            /data/memory.db (SQLite + WAL)                   │
│            /data/locks/* (flock safety)                     │
└─────────────────────────────────────────────────────────────┘
                         ▲
                         │ volume mount
                         │
            ┌────────────┴────────────┐
            │   memory-data volume   │
            └─────────────────────────┘
```

## Concurrency model

Three services share the same SQLite DB. The agentic-memory saga
acquires a `flock` at the top of every write (see `save/saga.py`),
so the services serialize through the OS-level flock — SQLite WAL
is the secondary write-order guarantee.

The lock files live at `/data/locks/<cron-name>.lock` (a
container-internal path that is part of the `memory-data` volume).
This is critical: the flock must be visible to all three services,
which is why we mount the data volume rather than separate paths
for each service.

## Cron service

`docker/cron_runner.py` reads `docker/schedule.json` and runs each
entry on its `interval_minutes`. The schedule mirrors the host
crontab entries from `cron/install_crontab.sh`, but uses intervals
instead of cron expressions for portability (no system cron daemon
needed).

`--once` mode runs all enabled entries once and exits — useful for
smoke tests in CI.

## Sync server security

`docker-compose.yml` reads these env vars (set via `.env` or
`docker compose --env-file`):

| Variable                   | Purpose                              |
|----------------------------|--------------------------------------|
| `MEMORY_SYNC_TOKEN`        | Bearer token (required in production)|
| `MEMORY_SYNC_HMAC_SECRET`  | HMAC-SHA256 body signature           |
| `MEMORY_SYNC_TLS_CERT`     | TLS cert (PEM)                       |
| `MEMORY_SYNC_TLS_KEY`      | TLS key (PEM)                        |
| `MEMORY_SYNC_CORS_ORIGINS` | Allowed CORS origins                 |

See `AGENTS.md` "Sync Server Security Model" for the deployment
tier guidance.

## Building with optional ML deps

```sh
docker build --build-arg INSTALL_EXTRAS=1 -t agentic-memory:full .
```

This pulls in `model2vec`, `usearch`, and `numpy` for semantic
search. The default slim build is enough for keyword search + KG
queries; the full build is needed for fact-level temporal queries
and cross-encoder reranking.

## Files in this directory

| File                    | Purpose                                      |
|-------------------------|----------------------------------------------|
| `Dockerfile`            | Multi-service image (at repo root)           |
| `docker-compose.yml`    | 3-service compose (at repo root)             |
| `.dockerignore`         | Exclude venv/, eval/, etc. (at repo root)    |
| `docker/entrypoint.sh`  | Selects mcp/sync/cron via $SERVICE           |
| `docker/cron_runner.py` | Python-based cron loop (replaces crontab)    |
| `docker/schedule.json`  | Cron schedule (replaces crontab entries)     |
