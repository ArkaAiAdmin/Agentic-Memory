# Agentic Memory Workflow Commands Reference

## Session Startup (Run at EVERY session start)

```bash
# Full initialization with task-specific search
python3 ~/.config/agentic-memory/agent_init.py "your task keywords"

# Quick initialization (just load index)
python3 ~/.config/agentic-memory/agent_init.py
```
## Session End Reflection (REQUIRED)

At the END of every session, run:
```bash
python3 ~/.config/agentic-memory/session_reflect.py <project_memory_dir>
```

This script:
- Shows files modified during the session
- Checks for unsaved context (files not in MEMORY.md, test artifacts in DB)
- Shows DB state (total memories, recent additions)
- Prints a MANDATORY checklist of things to save before yielding

**The agent MUST complete the checklist before ending the session.**

## MCP Tools (Available in Claude Code / OpenCode)

### Search & Discovery
| Tool | Description | Example |
|------|-------------|---------|
| `memory_search(query, limit=5)` | Search memories (re-ranked with fitness/importance) | `memory_search("android coroutines")` |
| `memory_semantic_search(query, limit=5)` | Semantic search (requires model2vec) | `memory_semantic_search("state management patterns")` |

### Writing Memories
| Tool | Description | Example |
|------|-------------|---------|
| `memory_save(content, category, title_slug, tags=[], pinned=false, is_global=false)` | Save new memory | `memory_save("Use Hilt for DI", "lessons", "hilt-di", ["android", "hilt"], true)` |
| `memory_reinforce(memory_ids=[], success=true)` | Reinforce/penalize memories after task | `memory_reinforce(["lessons/hilt-di"], true)` |

### Maintenance & Analysis
| Tool | Description | Example |
|------|-------------|---------|
| `memory_rebuild()` | Full index rebuild | `memory_rebuild()` |
| `memory_compact()` | Full compaction: tier migration + consolidation + rebuild | `memory_compact()` |
| `memory_audit()` | Health check report | `memory_audit()` |
| `memory_detect_contradictions()` | Find factual contradictions | `memory_detect_contradictions()` |
| `memory_rewrite_links()` | Fix wikilinks across files | `memory_rewrite_links()` |
| `memory_arc_stats()` | ARC cache statistics | `memory_arc_stats()` |
| `memory_review_schedule()` | Spaced repetition review schedule | `memory_review_schedule()` |

### Skill Management
| Tool | Description | Example |
|------|-------------|---------|
| `memory_compile_skill(lesson_slug, skill_name, description, when_to_use)` | Create skill from lesson | `memory_compile_skill("hilt-di", "AndroidHiltDI", "Hilt DI patterns", "Use for Android DI")` |

## CLI Commands (Direct Python)

```bash
# Search with re-ranking (local + global fallback)
python3 memory/search_memory.py "query" [limit] [--no-global]

# Full rebuild
python3 memory/rebuild_index.py memory memory/memory.db

# Link rewriting (with dry-run)
python3 memory/rewrite_links.py [--dry-run]

# Tier migration (with dry-run)
python3 memory/tier_migration.py [--dry-run]

# Fact consolidation
python3 memory/consolidate_facts.py

# Spaced repetition
python3 memory/spaced_repetition.py

# ARC cache stats
python3 memory/arc_cache.py
```

## Memory Categories

| Category | Purpose | Global? |
|----------|---------|---------|
| `projects/` | Active project context, specs | No |
| `lessons/` | Hard-won lessons, bug fixes | Yes (recommended) |
| `preferences/` | Workflow preferences, conventions | Yes |
| `decisions/` | Architecture Decision Records | Yes |
| `sessions/` | Session logs (auto-archived after 14 days) | No |
| `archive/` | Auto-archived cold memories | No |

## Frontmatter Schema (REQUIRED for all memories)

```yaml
---
created: 2026-06-04T10:30:00      # ISO datetime
updated: 2026-06-04T10:30:00      # ISO datetime
observed_at: 2026-06-04T10:30:00  # ISO datetime (last accessed)
tags: [android, kotlin, hilt]     # List of strings
pinned: false                     # Boolean - protected from decay/archival
importance: 3                     # 1-5 (5 = highest)
decay: standard                   # none, standard, fast
expires: 2026-12-31               # Optional TTL (date only)
related: [other-note.md]          # Cross-references
supersedes: old-note.md           # For versioned replacements
consolidation_state: working      # ephemeral, working, consolidated, permanent
---
```

## Cross-Project Workflow

1. **Save globally**: `memory_save(content, "lessons", "my-pattern", tags, pinned=true, is_global=true)`
2. **Search finds global automatically**: Local search falls back to global DB when results < 3
3. **Disable global**: `memory_search("query", include_global=false)` or CLI `--no-global`

## Weekly Maintenance Routine

```bash
# 1. Full compaction (tier migration + consolidation + rebuild)
memory_compact()

# 2. Check health
memory_audit()

# 3. Review spaced repetition
memory_review_schedule()

# 4. Check ARC stats
memory_arc_stats()

# 5. Review compaction proposals
cat memory/sessions/compaction-proposal.md
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Database not found" | Run `memory_rebuild()` or `python3 memory/rebuild_index.py memory memory/memory.db` |
| Search returns no results | Check spelling, try broader terms, use `--no-global` to isolate |
| Links not updating | Run `python3 memory/rewrite_links.py --dry-run` first, then without flag |
| Old date format warnings | Rebuild index - it will normalize date-only to ISO datetime |
| Global memories not visible | Ensure `memory/global` symlink exists in local memory dir |