# Agentic Memory

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-4041%20passed-brightgreen)](#testing)
[![SQLite FTS5](https://img.shields.io/badge/sqlite-FTS5-orange.svg)](https://www.sqlite.org/fts5.html)
[![MCP Tools](https://img.shields.io/badge/MCP-16%20tools_(15%20CORE%20%2B%201%20maintenance%20router)-purple.svg)](docs/reference/mcp-tools.md)
[![CRDT Sync](https://img.shields.io/badge/CRDT-field--level%20LWWES-green.svg)](docs/concepts/multi-agent-sync.md)
[![Temporal KG](https://img.shields.io/badge/Temporal-KG-brightgreen)](docs/concepts/temporal-kg.md)
[![v1.1.0](https://img.shields.io/badge/version-1.1.0-blue.svg)](CHANGELOG.md)

[Quick Start](#quick-start) · [5-Min Tutorial](#5-minute-tutorial) · [Features](#features) · [Architecture](#architecture) · [MCP Server](#mcp-server) · [Comparison](#comparison) · [Docs](docs/index.md) · [Contributing](CONTRIBUTING.md)

---

## What is Agentic Memory?

Agentic Memory gives AI agents **persistent, cross-session, local-first memory** — no cloud, no vendor lock-in, no API keys required. Memories are stored as human-readable Markdown files. A derived SQLite index enables fast full-text, semantic, and knowledge-graph search.

Built for **Claude Code**, **OpenCode**, and any MCP-compatible agent harness.

```
┌──────────────────────────────────────────────────────────────┐
│                        Agentic Memory                         │
├──────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌──────────────┐    ┌──────────────┐    ┌───────────────┐  │
│  │  Markdown    │───▶│  SQLite FTS5 │───▶│  BM25 Search  │  │
│  │  (source)    │    │  (derived)   │    │  + Vector     │  │
│  └──────────────┘    └──────────────┘    └───────────────┘  │
│         │                   │                    │           │
│         ▼                   ▼                    ▼           │
│  ┌──────────────┐    ┌──────────────┐    ┌───────────────┐  │
│  │  .md files   │    │  Knowledge   │    │  RRF Fusion   │  │
│  │  (Git-ready) │    │  Graph (KG)  │    │  Reranker     │  │
│  └──────────────┘    └──────────────┘    └───────────────┘  │
│                                                               │
│  36 cron jobs │ 6 hooks │ 16 MCP tools │ CRDT sync │ Arc cache │
└──────────────────────────────────────────────────────────────┘
```

---

## 5-Minute Tutorial

### Step 1 — Install (30 seconds)

```bash
pip install agentic-memory[all]
```

Or from source:

```bash
git clone https://github.com/ArkaAiAdmin/Agentic-Memory.git
cd Agentic-Memory
pip install -e ".[all]"
```

### Step 2 — Bootstrap your project (15 seconds)

```bash
agentic-memory init
```

This creates a `memory/` directory next to your project with categories: `lessons/`, `decisions/`, `projects/`, `preferences/`, `sessions/`.

### Step 3 — Save a memory (30 seconds)

```bash
agentic-memory search "how to handle auth errors"
# → "no results yet — your memory is empty"
```

Now save something:

```bash
agentic-memory search "save a memory about OAuth2 token refresh"
# Uses the MCP memory_save tool under the hood
```

Or via Python SDK:

```python
from agentic_memory import MemoryClient

client = MemoryClient()
client.save(
    content="OAuth2 access tokens expire after 1 hour. Always implement refresh_token grant. "
            "Store refresh tokens encrypted, never in plaintext logs.",
    category="lessons",
    tags=["auth", "oauth2", "security"],
    importance=4,
)
results = client.search("oauth refresh token")
print(results[0].content)
# → "OAuth2 access tokens expire after 1 hour..."
```

### Step 4 — Wire it to your agent (2 minutes)

Add to your MCP config (`~/.opencode/mcp-servers.json` or Claude Code `settings.json`):

```json
{
  "agentic-memory": {
    "command": "agentic-memory-server",
    "args": [],
    "env": {}
  }
}
```

That's it — your agent now has 15 CORE MCP tools: `memory_search`, `memory_save`, `memory_delete`, `memory_recall`, `memory_note`, `memory_learn`, `memory_audit`, `memory_organize`, `memory_share`, `memory_graph`, `memory_profile`, `memory_session_start`, `memory_advanced`, `memory_review_beliefs`, `memory_curate_autosave`. Plus `memory_maintenance` for 87 ADMIN + 3 DEPRECATED operations.

### Step 5 — Use it (ongoing)

```bash
agentic-memory search "database migration patterns"    # hybrid search
agentic-memory rebuild                                   # rebuild FTS5 index
agentic-memory consolidate                               # deduplicate
agentic-memory integrity                                 # health check
agentic-memory dashboard                                 # web UI
```

---

## Features

### Search — Best-in-Class Hybrid Retrieval

| Layer | Technology | Details |
|-------|-----------|---------|
| **FTS5 BM25** | SQLite FTS5 | 4 virtual tables, Porter stemmer |
| **Semantic vector** | model2vec + usearch HNSW | Optional `[embeddings]` extra |
| **Cross-encoder** | Qwen3-Reranker-0.6B | Opt-in via `deep_rerank=True` |
| **Knowledge graph** | SPO triples + entity graph | 3-hop Graph-RAG expansion |
| **RRF fusion** | Reciprocal Rank Fusion | Configurable k, multi-channel |
| **Temporal decay** | Ebbinghaus half-life | Default 180-day forget curve |
| **CTR feedback** | Click-through ranking | Channel weights learn from usage |
| **User profile** | Access-pattern adaptive | 90-day window, exponential decay |
| **Injection safety** | Prompt demotion | Suspicious content deprioritized |

### Write — Crash-Safe, Conflict-Preserving

- **`fcntl.flock` single-writer** — no two processes write simultaneously
- **Saga rollback** — 13-step indexed upsert; all steps roll back on failure
- **CRDT field-level LWWES** — concurrent edits to different fields both win
- **Conflict preservation** — `.conflict-<pid>-<ts>` files on collision, never silent overwrite
- **Safe atomic write** — POSIX rename, crash-safe `.md` persistence

### Knowledge Graph — Temporal + Contradiction-Aware

- Bi-temporal validity: `valid_from`, `valid_to`, `superseded_by`, `invalid_at`, `event_time`
- SPO triple extraction with confidence scores
- Entity dedup (exact + semantic)
- Contradiction detection (phrase + semantic)
- Graph CRDTs (v21): peer-to-peer entity/edge merge

### Automation — 36 Cron Jobs + 6 Hooks

- **Background worker** — SQLite-backed async task queue (flock-protected)
- **Cron jobs** — FTS rebuild, embedding recompute, KG backfill, quality filter, concept drift, integrity, tier migration, pinned decay, skill extraction, cross-session learning, and more
- **Lifecycle hooks** — PreToolUse, SessionStart, Stop/PostToolUse wired to Claude Code / OpenCode
- **Auto-save daemon** — inbox+daemon architecture, 2-5ms enqueue, 95% latency reduction

### Storage — Markdown Canonical, Git-Versionable

- `.md` files are the source of truth (not the DB)
- SQLite is derived and rebuildable
- No cloud dependency — all data stays local
- `safe_atomic_write` with conflict file preservation
- ~62 tables at schema v32, 32 versioned migrations

### MCP Surface — 16 Tools (15 CORE + 1 maintenance router)

Agents see **16 MCP tools** total:

```text
CORE (15 tools, always visible):
  memory_search, memory_save, memory_delete, memory_recall,
  memory_note, memory_learn, memory_audit, memory_organize,
  memory_share, memory_graph, memory_profile, memory_session_start,
  memory_advanced, memory_review_beliefs, memory_curate_autosave

MAINTENANCE (1 tool, router):
  memory_maintenance — exposes 87 ADMIN + 3 DEPRECATED operations
```

---

## Architecture

```
agentic-memory/                          # Repo root
├── agentic_memory/                      # Python package (pip installable)
│   ├── client.py                         # MemoryClient (save/search/CRUD)
│   ├── kg.py                             # KnowledgeGraph
│   ├── temporal.py                       # TemporalKG
│   ├── maintenance.py                    # Maintenance ops
│   ├── agent.py                          # AgentMemory (namespace-isolated)
│   ├── sync.py                           # SyncManager (CRDT sync)
│   ├── admin.py                          # Admin / circuit breaker
│   ├── models.py                         # 8 typed dataclasses
│   ├── exceptions.py                     # 9-class exception hierarchy
│   └── integrations/                     # LangChain + CrewAI adapters
├── save/                                # Write path subpackage
│   ├── indexers.py                       # FTS / embedding / chunk writes
│   ├── backlinks.py                      # Wiki-style backlink index
│   ├── crdt_helpers.py                   # CRDT snapshot extraction
│   └── post_save_hooks.py                # Fitness recalc, tier, audit
├── search/                              # Read path subpackage
│   ├── orchestrator.py                   # 11-phase search (1,811 LOC → 28 helpers)
│   ├── scoring.py                        # RRF, temporal decay, CTR
│   ├── rerankers.py                      # Cross-encoder, late interaction
│   └── chunk_index.py                    # Chunk search, Graph-RAG
├── backfill/                            # Index rebuild subpackage
├── cron/                                # 27 background job scripts
├── hooks/                               # 4 Claude Code lifecycle hooks
├── mcp_*.py (28 modules)                # Domain-split MCP tools
├── auto_save.py                         # Tool-call auto-save + daemon
├── background_worker.py                 # Async task processor
├── migration_runner.py                  # Schema v32, 32 migrations
└── db.py                                # Connection pool + WAL
```

**Production stats (2026-06-27):** ~90k LOC production, 234 test files, ~3,988 test functions, ~62-table SQLite schema at v32, 102 MCP tools, 36 cron jobs, 6 lifecycle hooks.

See [docs/architecture.md](docs/architecture.md) for full detail.

---

## Comparison

| Feature | Agentic Memory | Mem0 | Graphiti | Letta | EverOS |
|---------|---------------|------|----------|-------|--------|
| **Local-first** | ✅ (default) | ⚠️ partial | ⚠️ partial | ❌ | ✅ |
| **Markdown canonical** | ✅ | ❌ | ❌ | ❌ | ✅ |
| **Temporal KG** | ✅ bi-temporal | ✅ | ✅ | partial | ❌ |
| **Contradiction detection** | ✅ | ✅ | ✅ | ❌ | ❌ |
| **CRDT sync** | ✅ field-level | ❌ | ❌ | ❌ | ❌ |
| **FTS5 / BM25** | ✅ | ✅ | ❌ | ❌ | ❌ |
| **102 MCP tools** | ✅ | SDK only | MCP server | API | ❌ |
| **6 lifecycle hooks** | ✅ | ❌ | ❌ | ❌ | ❌ |
| **36 cron jobs** | ✅ | ❌ | ❌ | ❌ | ❌ |
| **Circuit breakers** | ✅ | ❌ | ❌ | ❌ | ❌ |
| **Field-level CRDT** | ✅ | ❌ | ❌ | ❌ | ❌ |
| **Schema migrations** | ✅ (v32) | ❌ | ❌ | ❌ | ❌ |
| **Test suite** | 3,988 tests | moderate | moderate | moderate | minimal |
| **License** | Apache 2.0 | Apache 2.0 | Apache 2.0 | Apache 2.0 | Apache 2.0 |

See [docs/explanation/comparison.md](docs/explanation/comparison.md) for detailed breakdowns.

---

## MCP Server

15 core tools are always visible to your agent; 84 admin + 3 deprecated tools are grouped under `memory_maintenance(operation="...")`:

```json
{
  "agentic-memory": {
    "command": "agentic-memory-server",
    "args": [],
    "env": {
      "MEMORY_KNOWLEDGE_GRAPH": "1",
      "MEMORY_EMBEDDINGS": "0",
      "MEMORY_DB_PATH": "./memory.db"
    }
  }
}
```

Full tool reference: [docs/reference/mcp-tools.md](docs/reference/mcp-tools.md)

---

## Configuration

### Install extras

```bash
pip install agentic-memory              # Core (MCP + SDK)
pip install agentic-memory[embeddings]  # + semantic search
pip install agentic-memory[reranker]    # + cross-encoder reranker
pip install agentic-memory[langchain]   # + LangChain adapters
pip install agentic-memory[crewai]      # + CrewAI adapters
pip install agentic-memory[dev]         # + pytest, ruff, mypy
pip install agentic-memory[all]         # Everything
```

### Key environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_DB_PATH` | `./memory.db` | Database path |
| `MEMORY_LOCAL_DIR` | `./memory` | Markdown memory directory |
| `MEMORY_KNOWLEDGE_GRAPH` | `0` | Enable KG extraction (1=on) |
| `MEMORY_EMBEDDINGS` | `0` | Enable semantic search (1=on) |
| `MEMORY_SYNC_TOKEN` | — | Required for sync server |
| `MEMORY_ASYNC_AUTOSAVE` | `1` | Inbox+daemon auto-save (0=sync) |

Full list: [docs/reference/configuration.md](docs/reference/configuration.md)

---

## Self-Hosting

### Docker Compose (recommended)

```bash
docker compose up -d
```

3 services: MCP server (stdio), sync server (TLS/mTLS, port 9877), cron scheduler. Named volume for data persistence. See [docker-compose.yml](docker-compose.yml).

### From source

```bash
git clone https://github.com/ArkaAiAdmin/Agentic-Memory.git
cd Agentic-Memory
python -m venv venv && source venv/bin/activate
pip install -e ".[all]"
agentic-memory-server  # starts MCP server
agentic-memory-dashboard  # web UI (Streamlit)
```

Detailed instructions: [docs/self-hosting.md](docs/self-hosting.md)

---

## CLI Reference

```bash
agentic-memory server         # Start MCP server
agentic-memory search "query" # Search memories (hybrid)
agentic-memory rebuild        # Rebuild FTS5 index
agentic-memory backfill       # Full index rebuild (--incremental or --full)
agentic-memory consolidate    # Deduplicate and merge
agentic-memory integrity      # Database health check
agentic-memory doctor         # Full health report
agentic-memory init           # (Re)bootstrap a project
agentic-memory dashboard      # Launch web dashboard (Streamlit)
agentic-memory status         # One-line health snapshot
agentic-memory-worker         # Process background tasks
```

---

## Documentation

| Section | What You'll Find |
|---------|-----------------|
| [Concepts](docs/concepts/why-markdown.md) | Why markdown, search pipeline, KG, tier system, background tasks, security model |
| [How-To Guides](docs/how-to/integrate-claude-code.md) | Claude Code / OpenCode integration, multi-project sharing, custom entities, debugging search, cron setup |
| [Reference](docs/reference/mcp-tools.md) | 102 registered MCP tools, configuration, database schema |
| [Explanation](docs/explanation/design-decisions.md) | Design rationale, comparison with alternatives, boot sequence |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, coding conventions, and PR guidelines.

Issues and PRs welcome. For security vulnerabilities, see [SECURITY.md](SECURITY.md).

---

## License

[Apache License 2.0](LICENSE)
