# How to Integrate with Claude Code

Set up Agentic Memory as an MCP server for Claude Code.

## Prerequisites

- Python 3.10+
- Agentic Memory installed (`pip install agentic-memory` or from source)
- Claude Code installed

## Step 1: Install Agentic Memory

```bash
# From PyPI
pip install agentic-memory

# Or from source
git clone https://github.com/ArkaAiAdmin/Agentic-Memory.git
cd Agentic-Memory
pip install -e ".[all]"
```

## Step 2: Configure MCP Server

Add the server to your Claude Code MCP config. The config file is at:
- **macOS**: `~/.opencode/mcp-servers.json`
- **Linux**: `~/.config/opencode/mcp-servers.json`

```json
{
  "agentic-memory": {
    "command": "agentic-memory-server",
    "args": [],
    "env": {}
  }
}
```

## Step 3: Enable Features

Set environment variables or edit `memory.toml`:

```bash
# Enable semantic search (optional, requires model2vec)
export MEMORY_EMBEDDINGS=1

# Enable knowledge graph
export MEMORY_KNOWLEDGE_GRAPH=1

# Enable fact consolidation
export MEMORY_CONSOLIDATION=1
```

Or in `~/.config/agentic-memory/memory.toml`:

```toml
[features]
embeddings = true
knowledge_graph = true
consolidation = true
```

## Step 4: Bootstrap a Project

```bash
cd ~/Assets/MyProject
bash ~/.config/agentic-memory/setup_memory.sh
```

This creates:
- `~/Assets/MyProject/memory/` directory structure
- Appends memory instructions to `AGENTS.md`

## Step 5: Verify

Start Claude Code and test:

```
> Save a memory: "Always use WAL mode for SQLite"
> Search for "SQLite WAL"
```

Or via MCP tools directly:

```python
memory_save(content="Always use WAL mode for SQLite", category="lessons")
memory_search(query="SQLite WAL")
```

## Available Tools

| Tool | Description |
|------|-------------|
| `memory_save` | Save a memory |
| `memory_search` | Search memories (hybrid BM25 + semantic) |
| `memory_get` | Get a specific memory by ID |
| `memory_delete` | Soft-delete a memory |
| `memory_rebuild` | Rebuild the search index |
| `memory_reinforce` | Reinforce a memory (positive/negative feedback) |
| `memory_compact` | Run deduplication and consolidation |
| `memory_audit` | Health check on the memory database |
| `memory_review_schedule` | Get spaced repetition review schedule |
| `memory_compile_skill` | Compile a lesson into an agent skill |
| `memory_session_summary` | Get session summary |
| `memory_tier_stats` | Get tier distribution stats |

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

### Slow performance

- Disable embeddings if not needed: `MEMORY_EMBEDDINGS=0`
- Reduce search limit: `memory_search(query="...", limit=5)`
- Rebuild the index periodically

## Further Reading

- [MCP Tools Reference](../reference/mcp-tools.md) — Full tool documentation
- [Configuration](../reference/configuration.md) — All environment variables
