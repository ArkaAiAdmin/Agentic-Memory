# Comparison

How Agentic Memory compares to other memory systems.

## Overview

| Feature | Agentic Memory | Mem0 | Zep | MemGPT/Letta |
|---------|---------------|------|-----|--------------|
| **Storage** | Markdown + SQLite | PostgreSQL | PostgreSQL | PostgreSQL |
| **Search** | 12-phase hybrid (BM25 + vector + ColBERT + RRF + cross-encoder + temporal decay + neural forget + KG boost) | Vector + graph | Vector + graph | Vector + LLM |
| **Source of truth** | Markdown files | Database | Database | Database |
| **Self-hosted** | Yes (default) | Yes | Yes | Yes |
| **Cloud option** | Planned | Yes | Yes | Yes |
| **LLM required** | No (optional) | Yes | Yes | Yes |
| **Privacy** | Local-first | Configurable | Configurable | Configurable |
| **MCP support** | Yes (17 CORE tools) | Yes | Yes | No |
| **Open source** | Apache 2.0 | Apache 2.0 | MIT | Apache 2.0 |

## Detailed Comparison

### Agentic Memory vs Mem0

**Mem0** is a popular memory system with cloud and self-hosted options.

| Aspect | Agentic Memory | Mem0 |
|--------|---------------|------|
| **Architecture** | Markdown-first, SQLite index | Database-first |
| **Search** | BM25 (fast, deterministic) | Vector (semantic) |
| **Knowledge graph** | Jaccard + regex NER | LLM-based extraction |
| **LLM dependency** | None (optional embeddings) | Required for extraction |
| **Pricing** | Free (self-hosted) | Free tier + paid plans |
| **Setup** | Single file, no server | Docker or cloud |

**When to choose Agentic Memory:**
- You want zero LLM dependency
- You need deterministic, reproducible search
- You prefer markdown files over databases
- You want local-first privacy

**When to choose Mem0:**
- You need cloud sync
- You want LLM-powered extraction
- You need a managed service
- You prefer graph-based search

### Agentic Memory vs Zep

**Zep** is a memory layer for AI assistants with strong knowledge graph support.

| Aspect | Agentic Memory | Zep |
|--------|---------------|-----|
| **Knowledge graph** | Jaccard + regex | LLM-based (more accurate) |
| **Search** | BM25 + vector | Vector + graph traversal |
| **Temporal awareness** | Tier system (hot/warm/cold) | Built-in time-aware search |
| **Setup** | Simple (SQLite) | Complex (PostgreSQL + Neo4j) |
| **Self-hosting** | Trivial | Docker Compose |

**When to choose Agentic Memory:**
- You want simple setup (no Neo4j)
- You prefer BM25 for keyword search
- You want markdown as source of truth
- You need lightweight deployment

**When to choose Zep:**
- You need accurate knowledge graph
- You want temporal-aware search
- You can handle more complex infrastructure
- You need enterprise features

### Agentic Memory vs MemGPT/Letta

**MemGPT/Letta** is a research project for LLM-based memory management.

| Aspect | Agentic Memory | MemGPT/Letta |
|--------|---------------|--------------|
| **Approach** | Deterministic extraction | LLM-based reasoning |
| **Search** | BM25 + vector | Vector + LLM |
| **Memory management** | Tier system (automated) | LLM decides what to keep |
| **Complexity** | Low (SQLite) | High (multiple LLM calls) |
| **Cost** | Free | LLM API costs |

**When to choose Agentic Memory:**
- You want predictable costs
- You need deterministic behavior
- You prefer simple infrastructure
- You want offline capability

**When to choose MemGPT/Letta:**
- You want LLM to manage memory
- You need complex reasoning about memory
- Cost is not a concern
- You're doing research

## Unique Advantages

### What Makes Agentic Memory Different

1. **Markdown as source of truth** — No other system does this
2. **Zero LLM dependency** — Works without any API calls
3. **BM25 as primary search** — Fast, deterministic, no model loading
4. **SQLite-only deployment** — No PostgreSQL, no Neo4j, no Redis
5. **Background task queue** — SQLite-backed, no external queue
6. **Tier system** — Automatic memory lifecycle management
7. **Injection detection** — Built-in safety without external tools

## When to Use What

### Use Agentic Memory if:
- You want local-first privacy
- You prefer markdown files
- You need fast, deterministic search
- You want zero infrastructure
- You're building a personal agent

### Use Mem0 if:
- You need cloud sync
- You want managed service
- You need LLM-powered features
- You're building a production app

### Use Zep if:
- You need accurate knowledge graph
- You can handle complex infrastructure
- You need enterprise features

### Use MemGPT/Letta if:
- You're doing research
- You want LLM-managed memory
- Cost is not a concern

## Further Reading

- [Design Decisions](design-decisions.md) — Why we made these choices
- [Architecture](../architecture/overview.md) — Full system design
