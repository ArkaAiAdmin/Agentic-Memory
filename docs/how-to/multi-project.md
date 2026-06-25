# How to Share Memories Across Projects

Agentic Memory supports **global memories** that are shared across all projects, and **local memories** that are project-specific.

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

## Further Reading

- [Why Markdown](../concepts/why-markdown.md) — Why markdown is the source of truth
- [Tier System](../concepts/tier-system.md) — How memories age across projects
