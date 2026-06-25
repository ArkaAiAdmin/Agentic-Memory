# Quick Start

Get Agentic Memory running in 5 minutes.

## Prerequisites

- Python 3.11+ (3.11, 3.12, or 3.14 — see `pyproject.toml` for the CI matrix)
- SQLite3 (with FTS5 support — included by default on all platforms)

## Installation

### macOS

**Homebrew (recommended):**

```bash
brew tap ArkaAiAdmin/agentic-memory https://github.com/ArkaAiAdmin/Agentic-Memory.git
brew install agentic-memory
```

**pip:**

```bash
pip install agentic-memory
```

### Windows

**pip:**

```powershell
pip install agentic-memory
```

**From source:**

```powershell
git clone https://github.com/ArkaAiAdmin/Agentic-Memory.git
cd Agentic-Memory
pip install -e .
```

**PowerShell usage:**

```powershell
# Start MCP server
agentic-memory-server

# Search memories
agentic-memory-search "query"

# Health check
agentic-memory-integrity
```

### Linux

**pip:**

```bash
pip install agentic-memory
```

**From source:**

```bash
git clone https://github.com/ArkaAiAdmin/Agentic-Memory.git
cd Agentic-Memory
pip install -e .
```

**Systemd service (optional):**

```bash
# Install as a system service
sudo cp ~/.config/agentic-memory/agentic-memory.service /etc/systemd/system/
sudo systemctl enable agentic-memory
sudo systemctl start agentic-memory
```

### All Platforms (from source)

```bash
git clone https://github.com/ArkaAiAdmin/Agentic-Memory.git
cd Agentic-Memory
pip install -e .
```

### Optional Features

```bash
# Semantic search (model2vec embeddings)
pip install agentic-memory[embeddings]

# Cross-encoder reranker
pip install agentic-memory[reranker]

# Everything
pip install agentic-memory[all]
```

## Bootstrap a Project

**macOS / Linux:**

```bash
cd ~/Assets/MyProject
bash ~/.config/agentic-memory/setup_memory.sh
```

**Windows (PowerShell):**

```powershell
cd ~\Assets\MyProject
bash ~/.config/agentic-memory/setup_memory.sh
```

This creates the memory directory structure and appends agent instructions to your `AGENTS.md`.

## CLI Commands

After installation, 11 CLI commands are available on all platforms (the table below has 12 rows because `agentic-memory-worker` is listed twice — see the note after the table):

| Command | Description |
|---------|-------------|
| `agentic-memory-server` | Start MCP server |
| `agentic-memory-search "query"` | Search memories |
| `agentic-memory-rebuild` | Rebuild FTS5 search index |
| `agentic-memory-backfill` | Rebuild all indexes (FTS5, embeddings, KG) |
| `agentic-memory-consolidate` | Deduplicate and merge facts |
| `agentic-memory-integrity` | Database health check |
| `agentic-memory-tier` | Migrate memories between tiers |
| `agentic-memory-compact` | Run full consolidation pipeline |
| `agentic-memory-bootstrap` | Initialize a project |
| `agentic-memory-sync` | Multi-agent CRDT sync (push/pull) |
| `agentic-memory-worker` | Process the background-task queue |

## Save Your First Memory

### Python API

```python
from agentic_memory import save_memory, search_memories

# Save a lesson
save_memory(
    content="Always use WAL mode for SQLite in concurrent applications",
    category="lessons",
    title_slug="sqlite-wal-mode",
)

# Search for it
results = search_memories("SQLite concurrency")
print(results[0]["content"])
```

### CLI

```bash
# Search
agentic-memory-search "SQLite WAL"

# Health check
agentic-memory-integrity

# Rebuild index
agentic-memory-rebuild
```

### MCP Server

Add to your MCP config:

```json
{
  "agentic-memory": {
    "command": "agentic-memory-server",
    "args": [],
    "env": {}
  }
}
```

Then use the `memory_save` and `memory_search` tools from your agent.

## What's Next?

- [Architecture](architecture.md) — How the system works
- [MCP Tools](reference/mcp-tools.md) — All 79 tools explained (15 CORE + 64 ADMIN)
- [Configuration](reference/configuration.md) — Environment variables and options
- [Self-Hosting](self-hosting.md) — Docker and deployment
