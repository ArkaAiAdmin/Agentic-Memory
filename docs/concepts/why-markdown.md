# Why Markdown?

Agentic Memory stores memories as **markdown files** — not just in a database. This is a deliberate architectural choice with significant implications.

## The Core Principle

> **Markdown files are the source of truth. SQLite is derived and rebuildable.**

If the database is corrupted, you delete it and run `rebuild_index.py`. Nothing is lost. If a markdown file is corrupted, you've lost a memory.

## Why Not "Just a Database"?

Most memory systems store everything in a database. This creates several problems:

| Problem | Database-Only | Markdown-First |
|---------|---------------|----------------|
| **Vendor lock-in** | Proprietary format, migration pain | Plain text, works everywhere |
| **Version control** | Binary blobs, no meaningful diffs | Human-readable diffs |
| **Inspectability** | Need a GUI or SQL queries | Open in any editor |
| **Backup** | Dump/restore procedures | Copy the directory |
| **Portability** | Export/import scripts | Move the folder |
| **AI readability** | Agent needs API calls | Agent reads the file |

## Markdown as Memory

When an agent saves a lesson, it writes a `.md` file:

```markdown
---
id: lessons/sqlite-wal-mode
category: lessons
type: lesson
resource: db_migrations.py
created: 2026-06-11T10:30:00Z
tags: [sqlite, database, concurrency]
tier: hot
---

# SQLite WAL Mode

Always use `PRAGMA journal_mode=WAL` for concurrent applications.
WAL allows multiple readers while a single writer is active.

## Context

Discovered during multi-agent session where two agents tried to
read/write simultaneously. Default journal mode causes "database is
locked" errors under concurrency.

## Evidence

- WAL mode: 100 concurrent reads, 1 write = no conflict
- Default mode: 2 reads + 1 write = "database is locked"
```

This file is:
- **Readable** — Any human can open it and understand the memory
- **Editable** — Fix typos, add context, merge related memories
- **Diffable** — `git diff` shows exactly what changed
- **Searchable** — `grep` finds patterns across all memories
- **Versionable** — Full history in git

## The Derived Layer

SQLite exists for **performance**, not as the source of truth:

```mermaid
graph TD
    A[Markdown files - truth] --> B[FTS5 index - fast search]
    A --> C[Knowledge graph - entity lookup]
    A --> D[Vector embeddings - semantic search]
    A --> E[Task queue - async processing]
```

Every derived artifact can be rebuilt:

```bash
# Rebuild everything from markdown
python rebuild_index.py --memory-dir /path/to/memory

# Rebuild just the knowledge graph
python backfill_all.py --mode full

# Rebuild just the vector index
python rebuild_vec_index.py
```

## When the Database Wins

The database layer provides capabilities markdown can't:

- **BM25 ranking** — Which memories are most relevant?
- **Hybrid search** — Combine keyword + semantic + graph
- **Fast lookups** — O(log n) vs O(n) for large memory stores
- **Cross-memory queries** — "Show all memories tagged with X from last week"

The trade-off is worth it: markdown for correctness and portability, SQLite for performance and features.

## Implications for Agents

Agents benefit from markdown-first in practical ways:

1. **No API calls to read** — Agent reads the file directly
2. **No export needed** — Agent can copy/share markdown files
3. **Audit-friendly** — Human reviewer reads the same file the agent wrote
4. **Merge-friendly** — Git handles conflicts naturally
5. **Portable across tools** — Works with Claude Code, OpenCode, or any text editor

## Further Reading

- [Architecture](../architecture/overview.md) — Full data flow diagram
- [Design Decisions](../explanation/design-decisions.md) — Why we chose this approach
