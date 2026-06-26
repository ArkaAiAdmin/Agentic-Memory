# Agentic Memory

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-3643%20passed-brightgreen)](#testing)
[![SQLite](https://img.shields.io/badge/sqlite-FTS5-orange.svg)](https://www.sqlite.org/fts5.html)

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
pip install agentic-memory
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
agentic-memory/                    # Repo root — 102 production modules, 42,373 LOC
├── agentic_memory/                # Python package (pip installable; 2 files)
│   ├── __init__.py                 # Re-exports Memory, AgentMemory, main
│   └── __main__.py                 # python -m agentic_memory
├── cli.py                          # 11 CLI entry points (server, search, rebuild, …)
├── save_pipeline.py                # Write path (1,359 LOC, shim → save/)
├── save/                           # Write path subpackage (5 modules, 1,251 LOC)
├── search_pipeline.py              # Read path (shim → search/)
├── search/                         # Read path subpackage (8 modules, 4,223 LOC)
├── backfill_all.py                 # Audit pipeline (shim → backfill/)
├── backfill/                       # Audit pipeline subpackage
├── auto_save.py                    # Tool-call auto-save hook + async daemon (1,700 LOC, 44 functions)
├── background_queue.py             # SQLite-backed task queue
├── background_worker.py            # Task queue worker (flock-protected, 120s timeout)
├── knowledge_graph.py              # Entity extraction
├── kg_dedup.py                     # Exact + semantic dedup
├── embedding_search.py             # Semantic search via model2vec
├── memory_injection.py             # Prompt injection detection
├── memory_common.py                # Shared utilities
├── db.py                           # Connection pool with re-entrancy guard
├── migration_runner.py             # Schema migrations (current v21, 21 migrations)
├── sync_server.py                  # HTTP sync server (native TLS + mTLS)
├── sync_client.py                  # HTTP sync client
├── memory_sharing.py               # In-DB memory sharing pool (was multi_agent.py)
├── adaptive_retention.py           # Psi-formula half-life + audit_hits cache
├── cron/                           # 26 background jobs (cron_*.py + install_crontab.sh)
├── mcp_*.py (26 modules)           # Domain-split MCP tools (85 total: 15 CORE + 70 ADMIN)
└── ...
```

**Top-level scale (2026-06-23):** 102 Python modules, 46,247 root-level LOC (56,799 including all subpackage files). Test suite: 183 test files, 3,494 test functions, all passing (10 skipped for speed), 0 warnings. ~51-table SQLite schema at version 21 (added v18 fact-level temporal KG, v19 entity FK fix, v20 kg_facts FTS5 index, v21 kg_crdt tables).

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
pip install agentic-memory              # Core only (MCP server)
pip install agentic-memory[embeddings]  # + semantic search
pip install agentic-memory[reranker]    # + cross-encoder reranker
pip install agentic-memory[dev]         # + pytest, ruff, mypy
pip install agentic-memory[all]         # Everything
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

- [x] Core memory system (markdown + SQLite FTS5)
- [x] Knowledge graph with entity extraction
- [x] Background task queue
- [x] Semantic entity resolution
- [x] pip package (`pip install agentic-memory`)
- [x] Homebrew tap for macOS
- [ ] Web API server (FastAPI)
- [ ] Python SDK
- [ ] LangChain / CrewAI integrations
- [ ] Managed cloud service

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

[Apache License 2.0](LICENSE)
