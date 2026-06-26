# Agentic Memory

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-3643%20passed-brightgreen)](#testing)
[![SQLite](https://img.shields.io/badge/sqlite-FTS5-orange.svg)](https://www.sqlite.org/fts5.html)
[![v1.1.0](https://img.shields.io/badge/version-1.1.0-blue.svg)](CHANGELOG.md)

[Quick Start](#quick-start) | [Documentation](docs/index.md) | [Architecture](#architecture) | [MCP Server](#mcp-server) | [Self-Host](#self-hosting) | [Contributing](CONTRIBUTING.md)

---

## What is Agentic Memory?

Agentic Memory gives AI agents persistent, cross-session memory — without cloud dependencies, vendor lock-in, or losing control of your data.

Agents save lessons, decisions, and patterns to **markdown files** (the source of truth). A derived **SQLite FTS5** index enables fast full-text search. An optional **knowledge graph** captures entities and relationships. A background **task queue** handles expensive operations asynchronously.

```
+----------------+     +---------------+     +--------------+
|   Markdown     |---->|  SQLite FTS5  |---->|  Search API  |
|   (truth)      |     |  (derived)    |     |  (BM25+vec)  |
+----------------+     +---------------+     +--------------+
                              |
                       +------+------+
                       |  Knowledge  |
                       |    Graph    |
                       +-------------+
```

## Quick Start

### Install

**macOS (Homebrew):**

```bash
brew tap ArkaAiAdmin/agentic-memory https://github.com/ArkaAiAdmin/Agentic-Memory.git
brew install agentic-memory
```

**pip (all platforms):**

```bash
pip install agentic-memory              # Core + SDK + MCP server
pip install agentic-memory[embeddings]  # + semantic search
pip install agentic-memory[reranker]    # + cross-encoder reranker
pip install agentic-memory[langchain]   # + LangChain adapters
pip install agentic-memory[crewai]      # + CrewAI adapters (Python 3.11–3.13)
pip install agentic-memory[all]         # Everything
```

**From source:**

```bash
git clone https://github.com/ArkaAiAdmin/Agentic-Memory.git
cd Agentic-Memory
pip install -e .
```

### Bootstrap a project

```bash
cd ~/Assets/MyProject
bash ~/.config/agentic-memory/setup_memory.sh
```

### Use CLI Commands

After installation, 11 CLI commands are available (`cli.py` defines 11; `pyproject.toml [project.scripts]` currently exposes 10 — `agentic-memory-sync` was added 2026-06-22 but the package must be reinstalled for the script to be on `$PATH`):

```bash
agentic-memory-server         # Start MCP server
agentic-memory-search "query" # Search memories
agentic-memory-rebuild        # Rebuild search index
agentic-memory-backfill       # Rebuild all indexes
agentic-memory-consolidate    # Deduplicate and merge
agentic-memory-integrity      # Database health check
agentic-memory-tier           # Tier migration
agentic-memory-compact        # Run consolidation pipeline
agentic-memory-bootstrap      # Initialize a project
agentic-memory-worker         # Process background tasks
```

### Use as MCP Server

Add to your MCP config (`~/.opencode/mcp-servers.json` or Claude Code settings):

```json
{
  "agentic-memory": {
    "command": "agentic-memory-server",
    "args": [],
    "env": {}
  }
}
```

15 core tools available: `memory_save`, `memory_search`, `memory_semantic_search`, `memory_facts_search`, `memory_graph_search`, `memory_recall_context`, `memory_session_start`, `memory_user_profile`, `memory_delete`, `memory_restore`, `memory_check_contradictions`, `memory_scan_injection`, `memory_rebuild`, `memory_supersede`, `memory_profile_access`. Plus `memory_maintenance` grouped tool for 64 admin operations.

## Features

| Feature | Description |
|---------|-------------|
| **Markdown-first** | Memories stored as human-readable `.md` files — version-controllable, diffable, portable |
| **SQLite FTS5 search** | Fast full-text search with BM25 ranking — no external search engine needed |
| **Semantic search** | Optional vector embeddings via `model2vec` for meaning-based retrieval |
| **Knowledge graph** | Entity extraction and relationship tracking across sessions |
| **Background tasks** | SQLite-backed queue for async entity resolution, fact consolidation, contradiction detection |
| **Spaced repetition** | SM-2 algorithm surfaces memories at optimal review intervals |
| **Cross-project sharing** | Global memories symlinked into every project via `MEMORY.md` index |
| **MCP integration** | 15 core tools + `memory_maintenance` grouped tool (64 admin ops) for Claude Code, OpenCode, and any MCP-compatible agent |
| **Native TLS** | Optional `MEMORY_SYNC_TLS_CERT` + `MEMORY_SYNC_TLS_KEY` (mTLS via `MEMORY_SYNC_TLS_CLIENT_CA`) for the sync server — no reverse proxy required |
| **Zero dependencies** | Core system works with just Python stdlib + SQLite — no cloud, no API keys |
| **Typed Python SDK** | 7 public classes (`MemoryClient`, `AgentMemory`, `KnowledgeGraph`, `TemporalKG`, `Maintenance`, `SyncManager`, `Admin`), 8 dataclasses, 9 exception types — `pip install agentic-memory` |
| **LangChain adapters** | `AgenticMemoryRetriever`, `AgenticMemoryChatHistory`, structured tools, callback handler — `pip install agentic-memory[langchain]` |
| **CrewAI adapters** | `AgenticMemorySearchTool`, `AgenticMemorySaveTool`, crew memory slot adapter — `pip install agentic-memory[crewai]` (Python 3.11–3.13) |

## Documentation

Full documentation at [docs/index.md](docs/index.md):

| Section | What You'll Find |
|---------|-----------------|
| [Concepts](docs/concepts/why-markdown.md) | Why markdown, how search works, knowledge graph, tier system, background tasks |
| [How-To Guides](docs/how-to/integrate-claude-code.md) | Claude Code integration, multi-project sharing, custom entity types, debugging |
| [Reference](docs/reference/mcp-tools.md) | MCP tools, configuration, database schema |
| [Explanation](docs/explanation/design-decisions.md) | Design rationale, comparison with alternatives |

## Architecture

```
agentic-memory/                          # Repo root — production + tests + integrations
├── agentic_memory/                      # Python package (pip installable)
│   ├── __init__.py                       # Re-exports all SDK classes + models
│   ├── client.py                         # MemoryClient (core save/search/CRUD)
│   ├── kg.py                             # KnowledgeGraph (entity/fact/path ops)
│   ├── temporal.py                       # TemporalKG (time-aware queries, contradictions)
│   ├── maintenance.py                    # Maintenance (rebuild, compact, audit, health)
│   ├── agent.py                          # AgentMemory (namespace-isolated saves)
│   ├── sync.py                           # SyncManager (CRDT sync + cross-agent sharing)
│   ├── admin.py                          # Admin (health, circuit breaker)
│   ├── models.py                         # 8 typed dataclasses (MemoryResult, Fact, …)
│   ├── exceptions.py                     # 9-class exception hierarchy
│   ├── utils.py                          # DB path, connection pool, result parsing
│   └── integrations/                     # Ecosystem adapters (lazy-guarded)
│       ├── langchain/
│       │   ├── retriever.py              # AgenticMemoryRetriever (BaseRetriever)
│       │   ├── history.py                # AgenticMemoryChatHistory (BaseChatMessageHistory)
│       │   ├── tool.py                   # search_tool + save_tool (StructuredTool)
│       │   └── callback.py               # AgenticMemoryCallbackHandler
│       └── crewai/
│           ├── tool.py                   # AgenticMemorySearchTool + SaveTool (BaseTool)
│           └── memory.py                 # AgenticMemoryMemory (crew memory slot)
├── save_pipeline.py                     # Write path shim → save/
├── save/                                # Write path subpackage (5 modules)
├── search_pipeline.py                   # Read path shim → search/
├── search/                              # Read path subpackage (8 modules)
├── mcp_*.py (26 modules)                # Domain-split MCP tools (85 total)
├── auto_save.py                         # Tool-call auto-save hook + async daemon
├── background_queue.py                  # SQLite-backed task queue
├── background_worker.py                 # Task queue worker (flock-protected)
├── sync_server.py                       # HTTP sync server (native TLS + mTLS)
├── db.py                                # Connection pool with re-entrancy guard
├── migration_runner.py                  # Schema migrations (v21, 21 migrations)
└── ...
```

**Key stats (2026-06-26):** 102 production modules at repo root. Typed SDK: 11 modules, ~2,800 LOC. Test suite: 196 test files, 3,623 passing, 0 failures, 20 skipped. ~51-table SQLite schema at version 21 (added v18 fact-level temporal KG, v19 entity FK fix, v20 kg_facts FTS5 index, v21 kg_crdt tables).

### Per-project layout

```
~/.config/agentic-memory/          # Global config
├── memory.toml                    # Configuration
├── setup_memory.sh                # Project bootstrapper
└── memory.db                      # Global shared memories (derived)

~/Assets/ProjectName/memory/       # Per-project
├── MEMORY.md                      # Central index (derived from DB)
├── memory.db                      # Local search index (derived)
├── lessons/                       # Technical lessons
├── decisions/                     # Architecture Decision Records
├── projects/                      # Project context
├── preferences/                   # Developer preferences
├── quirks/                        # Known issues
└── sessions/                      # Session logs
```

### Key Principles

- **Markdown is source of truth** — SQLite is derived, rebuildable via `rebuild_index.py`
- **One-directional data flow** — markdown -> index, never reversed
- **No LLM in the write path** — deterministic extraction only
- **Graceful degradation** — system works without any process running
- **Local-first** — all data stays on your machine

## Self-Hosting

### Docker

```bash
docker compose up -d
```

This starts the MCP server on `http://localhost:8080`.

### From source

```bash
git clone https://github.com/ArkaAiAdmin/Agentic-Memory.git
cd Agentic-Memory
python -m venv venv && source venv/bin/activate
pip install -e ".[all]"
agentic-memory-server  # Starts MCP server
```

See [docs/self-hosting.md](docs/self-hosting.md) for detailed instructions.

## Configuration

### Install extras

```bash
pip install agentic-memory              # Core only (MCP server + typed SDK)
pip install agentic-memory[embeddings]  # + semantic search (model2vec + usearch)
pip install agentic-memory[reranker]    # + cross-encoder reranker (torch + transformers)
pip install agentic-memory[langchain]   # + LangChain adapters (retriever, history, tools, callback)
pip install agentic-memory[crewai]      # + CrewAI adapters (tools, memory slot; Python 3.11–3.13)
pip install agentic-memory[dev]         # + pytest, ruff, mypy
pip install agentic-memory[all]         # Everything except crewai on Python 3.14+
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_DB_PATH` | `./memory.db` | Override database path |
| `MEMORY_LOCAL_DIR` | `./memory` | Override local memory directory |
| `MEMORY_CONSOLIDATION` | `0` | Enable fact consolidation (1=on) |
| `MEMORY_KNOWLEDGE_GRAPH` | `0` | Enable knowledge graph (1=on) |
| `MEMORY_EMBEDDINGS` | `0` | Enable semantic embeddings (1=on) |

## Database Operations

```bash
# Health check
agentic-memory-integrity

# Rebuild indexes
agentic-memory-rebuild               # Rebuild FTS5 index
agentic-memory-backfill              # Rebuild all indexes (FTS5, embeddings, KG)

# Maintenance
agentic-memory-consolidate           # Deduplicate and merge
agentic-memory-tier                  # Migrate memories between tiers
agentic-memory-compact               # Run full consolidation pipeline

# Background tasks
agentic-memory-worker                # Process pending background tasks
```

## Roadmap

### v1.0.0 — Ecosystem Integration Layer (shipped 2026-06-26)

- [x] Core memory system (markdown + SQLite FTS5)
- [x] Knowledge graph with entity extraction
- [x] Background task queue + cron workers
- [x] Semantic entity resolution
- [x] Typed Python SDK (`pip install agentic-memory` — 7 classes, 8 dataclasses)
- [x] LangChain adapters (`pip install agentic-memory[langchain]`)
  - `AgenticMemoryRetriever` — drops into any `BaseRetriever` / `RetrievalQA` chain
  - `AgenticMemoryChatHistory` — `BaseChatMessageHistory` with role tagging
  - `search_tool` + `save_tool` — `StructuredTool` instances for ReAct agents
  - `AgenticMemoryCallbackHandler` — auto-persist every LLM turn
- [x] CrewAI adapters (`pip install agentic-memory[crewai]`, Python 3.11–3.13)
  - `AgenticMemorySearchTool` + `AgenticMemorySaveTool` — `BaseTool` subclasses
  - `AgenticMemoryMemory` — drop-in crew `memory` slot adapter
- [x] MCP server (15 core tools + 64 admin ops via `memory_maintenance`)
- [x] 3,603 passing tests, 0 failures across 196 test files

### v1.1.0 — Pipeline Correctness (shipped 2026-06-26)

- [x] Fix: search API now returns `content` (was always `""` in `result_items`)
- [x] Documented: Python 3.14 / CrewAI limitation (upstream `tiktoken` wheel gap)

### Planned

- [ ] Web API server (FastAPI)
- [ ] LlamaIndex adapter (`pip install agentic-memory[llamaindex]`)
- [ ] Haystack document store connector
- [ ] Managed cloud service
- [ ] PyPI publication with download counter and docs site

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

[Apache License 2.0](LICENSE)
