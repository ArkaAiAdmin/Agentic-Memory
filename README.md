# Agentic Memory System

A local-first, markdown-primary persistent memory system for AI agents. Zero cloud dependency. SQLite is derived and rebuildable. Markdown files are the source of truth.

## What It Does

Gives AI agents persistent cross-session memory. Agents can save lessons, decisions, and patterns to markdown files. A derived SQLite FTS5 index enables fast search. A central MEMORY.md index bootstraps new sessions.

## Architecture

```
~/.config/agentic-memory/          # Global config (shared across projects)
├── *.py                           # Core scripts
├── setup_memory.sh                # Project bootstrapper
├── memory_workflow.md             # Workflow reference
├── venv/                          # Python virtual environment
└── memory.db                      # Global shared memories (derived)

~/Assets/ProjectName/memory/       # Per-project (symlinked to global)
├── MEMORY.md                      # Central index (derived from DB)
├── memory.db                      # Local search index (derived)
├── global/ -> ~/.config/...       # Symlink to global config
├── lessons/                       # Technical lessons
├── decisions/                     # Architecture Decision Records
├── projects/                      # Project context
├── preferences/                   # Developer preferences
├── quirks/                        # Known issues
└── sessions/                      # Session logs
```

## Key Principles

- **Markdown is source of truth** — SQLite is derived, rebuildable via `rebuild_index.py`
- **One-directional data flow** — markdown → index, never reversed
- **No LLM in the write path** — deterministic extraction only
- **Graceful degradation** — system works without any process running
- **Cross-project sharing** — global memories symlinked into every project

## Quick Start

```bash
# Bootstrap in any project
cd ~/Assets/MyProject
bash ~/.config/agentic-memory/setup_memory.sh

# Or auto-setup on cd (add to .zshrc)
# The chpwd hook detects new projects automatically
```

## Scripts

| Script | Purpose |
|--------|---------|
| `setup_memory.sh` | Bootstrap memory system in a project |
| `rebuild_index.py` | Rebuild SQLite DB + MEMORY.md from markdown files |
| `search_memory.py` | CLI search with BM25 + fitness re-ranking |
| `memory_mcp.py` | MCP server (12 tools for Claude Code / OpenCode) |
| `session_reflect.py` | End-of-session reflection checklist |
| `consolidate_facts.py` | Async fact consolidation (contradiction detection) |
| `rewrite_links.py` | Normalize `[[wikilinks]]` across markdown files |
| `agent_init.py` | Session startup initialization |
| `tier_migration.py` | Memory tier migration |
| `spaced_repetition.py` | spaced repetition scheduling |
| `embedding_search.py` | Optional semantic search (model2vec) |
| `progressive_summarize.py` | Progressive summarization |
| `contradiction_detector.py` | Detect conflicting facts |
| `arc_cache.py` | ARC cache implementation |
| `memory_common.py` | Shared utilities |

## Agent Instructions

The `setup_memory.sh` script appends memory system instructions to your project's `AGENTS.md` and `CLAUDE.md`. These tell agents to:

1. **Session start** — Search memories before answering any question
2. **During work** — Save lessons, decisions, and patterns immediately
3. **Session end** — Run reflection checklist before yielding

## MCP Server

Register in your MCP config (`~/.opencode/mcp-servers.json` or Claude Code settings):

```json
{
  "agentic-memory": {
    "command": "python3",
    "args": ["~/.config/agentic-memory/memory_mcp.py"],
    "env": { "PYTHONPATH": "~/.config/agentic-memory" }
}
```

12 tools: `memory_save`, `memory_search`, `memory_rebuild`, `memory_reinforce`, `memory_compact`, `memory_audit`, `memory_review_schedule`, `memory_compile_skill`, `memory_session_summary`, `memory_cross_project_search`, `memoryigrate_tier`, `memory_get`.

## Requirements

- Python 3.10+
- SQLite3 with FTS5 support
- No external dependencies for core functionality

## License

MIT
