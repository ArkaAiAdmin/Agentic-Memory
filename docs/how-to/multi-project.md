# How to Share Memories Across Projects

## Goal

Save and search memories across multiple projects — keep universal lessons in a global store and project-specific patterns in local stores.

## Prerequisites

- [ ] Agentic Memory installed (follow the [integrate-claude-code](integrate-claude-code.md) guide)
- [ ] At least one project bootstrapped with `setup_memory.sh`
- [ ] A second project directory you want to share memories with

## Architecture

```
~/.config/agentic-memory/
├── memory.db              # Global database
├── memory/                # Global markdown files
│   ├── lessons/
│   ├── decisions/
│   └── ...
└── venv/

~/Assets/ProjectA/
├── memory/
│   ├── memory.db          # Local database
│   ├── lessons/           # Local memories
│   └── MEMORY.md          # Symlink to global index
└── AGENTS.md

~/Assets/ProjectB/
├── memory/
│   ├── memory.db
│   ├── lessons/
│   └── MEMORY.md          # Same global index
└── AGENTS.md
```

## Saving Global vs Local Memories

### Local (default)

```python
from agentic_memory import save_memory

# Saved to ProjectA/memory/
save_memory(
    content="Use async/await for I/O operations in this project",
    category="lessons",
    title_slug="async-io-pattern",
    is_global=False,  # Default
)
```

### Global

```python
# Saved to ~/.config/agentic-memory/memory/
save_memory(
    content="Always use PRAGMA journal_mode=WAL for SQLite",
    category="lessons",
    title_slug="sqlite-wal-mode",
    is_global=True,  # Shared across all projects
)
```

### Via CLI

```bash
# Local memory
python search_memory.py save \
  --content "Project-specific pattern" \
  --category lessons \
  --title-slug project-pattern

# Global memory
python search_memory.py save \
  --content "Universal best practice" \
  --category lessons \
  --title-slug universal-practice \
  --global
```

## Searching Across Projects

### Local search (default)

```python
from agentic_memory import search_memories

# Only searches ProjectA's memories
results = search_memories("async patterns", include_global=False)
```

### Global + Local search

```python
# Searches both local and global memories
results = search_memories("SQLite patterns", include_global=True)
```

## Setting Up Multi-Project

### Option 1: Bootstrap each project

```bash
# For each project
cd ~/Assets/ProjectA
bash ~/.config/agentic-memory/setup_memory.sh

cd ~/Assets/ProjectB
bash ~/.config/agentic-memory/setup_memory.sh
```

### Option 2: Symlink global memories

```bash
# Create a symlink to global memory directory
ln -s ~/.config/agentic-memory/memory ~/Assets/ProjectA/shared-memory
```

### Option 3: Use the MEMORY.md index

The `MEMORY.md` file in each project's memory directory is a derived index that includes both local and global memories. It's automatically updated when you save memories.

## Verification

```bash
# Save a global memory
python search_memory.py save \
  --content "Global test memory" \
  --category lessons \
  --title-slug global-test \
  --global

# Search from a different project — the global memory should appear
cd ~/Assets/ProjectB
python search_memory.py "global test" --global
```

Expected output: The search result includes the global test memory regardless of which project directory you query from.

## Troubleshooting

### Global memory not appearing in other projects

**Cause**: The `include_global` flag was not set during search.
**Fix**: Pass `include_global=True` or `--global` to include global results.

### Memory saved as local by mistake

**Cause**: Default `is_global=False` saved the memory to the local project store.
**Fix**: Re-save with `is_global=True` or delete the local copy and re-save globally.

## Configuration

| Variable | Default | Effect |
|----------|---------|--------|
| `MEMORY_DB_PATH` | `./memory.db` | Override database path |
| `MEMORY_LOCAL_DIR` | `./memory` | Override local memory directory |

## Best Practices

1. **Use global for universal lessons** — Things that apply everywhere (e.g., "use WAL mode")
2. **Use local for project-specific** — Things specific to this codebase (e.g., "our auth uses JWT")
3. **Don't overuse global** — Too many global memories dilute local relevance
4. **Review global periodically** — Archive outdated global memories

## Related

- [Integrate with Claude Code](integrate-claude-code.md) — Initial setup
- [Why Markdown](../concepts/why-markdown.md) — Why markdown is the source of truth
- [Tier System](../concepts/tier-system.md) — How memories age across projects
- [Multi-Agent Sync](../concepts/multi-agent-sync.md) — Sync memories across agents
