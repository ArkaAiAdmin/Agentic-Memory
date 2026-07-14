# Comparison

## Context

When choosing a memory system for your agent, you have several options — each with different tradeoffs around privacy, infrastructure, LLM dependency, and search quality. This document compares Agentic Memory to the most popular alternatives so you can make an informed choice based on your specific requirements.

## Competitor Landscape (July 2026)

| System | Funding | LOC (approx) | License | Status |
|--------|---------|---------------|---------|--------|
| **Agentic Memory** | — | ~110K | Apache 2.0 | Active development |
| **Mem0** | $8M raised | ~30K | Apache 2.0 | Active |
| **Letta** (fka MemGPT) | $28M raised | ~50K | Apache 2.0 | Active |
| **Zep** | $5.5M raised | ~40K | Apache 2.0 | Active |
| **Pinecone** | $138M raised | N/A (proprietary) | Proprietary | Managed service |
| **Weaviate** | $67.7M raised | ~200K | BSD-3 | Active |
| **Qdrant** | $47.5M raised | ~150K | Apache 2.0 | Active |

## Overview

| Feature | Agentic Memory | Mem0 | Zep | MemGPT/Letta |
|---------|---------------|------|-----|--------------|
| **Storage** | Markdown + SQLite | PostgreSQL | PostgreSQL | PostgreSQL |
| **Search** | 14-phase hybrid (BM25 + vector + ColBERT + RRF + cross-encoder + temporal decay + neural forget + KG boost) | Vector + graph | Vector + graph | Vector + LLM |
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
8. **14-phase hybrid search** — BM25 + vector + ColBERT + RRF + cross-encoder + temporal decay + neural forget + KG boost
9. **Field-level CRDT** — Concurrent edits to different fields both win
10. **Temporal knowledge graph** — Bi-temporal validity with contradiction detection

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

### Agentic Memory vs Pinecone

**Pinecone** is a managed vector database service ($138M raised, proprietary).

| Aspect | Agentic Memory | Pinecone |
|--------|---------------|----------|
| **Architecture** | Markdown + SQLite | Managed vector DB |
| **Search** | 14-phase hybrid | Vector similarity only |
| **Self-hosted** | Yes (default) | No (managed only) |
| **LLM required** | No | No |
| **Pricing** | Free (self-hosted) | Pay-per-query |
| **Privacy** | Local-first | Cloud-only |

**When to choose Agentic Memory:**
- You want local-first privacy
- You need hybrid search (BM25 + vector + KG)
- You want zero infrastructure costs
- You need knowledge graph capabilities

**When to choose Pinecone:**
- You need managed infrastructure
- You want vector-only similarity search
- You have high-throughput requirements
- You prefer SaaS over self-hosted

### Agentic Memory vs Weaviate

**Weaviate** is an open-source vector database ($67.7M raised, BSD-3, ~200K LOC).

| Aspect | Agentic Memory | Weaviate |
|--------|---------------|----------|
| **Architecture** | Markdown + SQLite | Vector DB + modules |
| **Search** | 14-phase hybrid | Vector + BM25 + hybrid |
| **Self-hosted** | Yes (trivial) | Yes (Docker) |
| **LLM required** | No | Optional (generative modules) |
| **Setup** | Single file | Docker Compose |
| **Scale** | Single-node | Distributed |

**When to choose Agentic Memory:**
- You want simple deployment (no Docker)
- You need Markdown as source of truth
- You want local-first privacy
- You need knowledge graph capabilities

**When to choose Weaviate:**
- You need distributed vector search
- You want built-in ML modules
- You need high-throughput production deployment
- You prefer a established vector DB ecosystem

### Agentic Memory vs Qdrant

**Qdrant** is an open-source vector database ($47.5M raised, Apache 2.0, ~150K LOC).

| Aspect | Agentic Memory | Qdrant |
|--------|---------------|--------|
| **Architecture** | Markdown + SQLite | Vector DB |
| **Search** | 14-phase hybrid | Vector + payload filtering |
| **Self-hosted** | Yes (trivial) | Yes (Docker) |
| **LLM required** | No | No |
| **Setup** | Single file | Docker |
| **Performance** | Good for small-medium | Optimized for large scale |

**When to choose Agentic Memory:**
- You want zero infrastructure
- You need hybrid search (BM25 + vector + KG)
- You want Markdown as source of truth
- You need knowledge graph capabilities

**When to choose Qdrant:**
- You need high-performance vector search
- You want payload-based filtering
- You need distributed deployment
- You prefer a mature vector DB

## Tradeoffs

Choosing Agentic Memory means accepting certain constraints:

- **No LLM-powered extraction** — entity extraction uses regex + Jaccard matching, which is less accurate than LLM-based approaches (Mem0, Zep) but is deterministic and requires zero API costs.
- **BM25-first search** — keyword precision is excellent, but semantic matching requires the optional vector search pipeline. Other systems default to semantic search.
- **SQLite concurrency** — single-writer means one process writes at a time. PostgreSQL-based systems (Mem0, Zep) handle higher write concurrency natively.
- **No cloud sync (default)** — sync is opt-in via the multi-agent CRDT layer. Mem0 and Zep offer managed cloud sync out of the box.
- **Manual infrastructure decisions** — tier migration, index rebuilding, and compaction are handled by cron jobs. Other systems manage this automatically.

## Implications

For **evaluators**: use the "When to choose" guides under each comparison to map your requirements to the right system. If you need zero infrastructure and local-first privacy, start with Agentic Memory. If you need managed cloud sync, look at Mem0.

For **operators**: Agentic Memory's simplicity (SQLite, no server process) means less operational overhead than alternatives that require PostgreSQL, Neo4j, or Docker Compose. The tradeoff is that advanced features (knowledge graph, entity resolution) are less sophisticated.

For **migrators**: Agentic Memory's markdown-first design means you can bulk-import from any system that can export to markdown. The reverse path (migrating away) is equally straightforward — your memories are plain text files.

## Related

- [Design Decisions](design-decisions.md) — Why we made these choices
- [Architecture](../architecture/overview.md) — Full system design
- [MCP Tools Reference](../reference/mcp-tools.md) — All available MCP tools
