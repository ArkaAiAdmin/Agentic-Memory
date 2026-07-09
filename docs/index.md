# Agentic Memory Documentation

Local-first persistent memory for AI agents. Zero cloud dependency, MCP-native, privacy-first.

## Quick Links

- [Quick Start](guides/quick-start.md) — Get running in 5 minutes
- [Python SDK](api/python-sdk.md) — Full API reference
- [TypeScript SDK](api/typescript-sdk.md) — Full API reference
- [REST API](api/rest-api.md) — HTTP endpoints
- [Architecture](architecture/overview.md) — System design

## Guides

| Guide | Description |
|-------|-------------|
| [Quick Start](guides/quick-start.md) | Get running in 5 minutes |
| [LangChain](guides/langchain.md) | LangChain integration |
| [CrewAI](guides/crewai.md) | CrewAI integration |

## API Reference

| API | Description |
|-----|-------------|
| [Python SDK](api/python-sdk.md) | `MemoryClient`, `AgentMemory`, models, exceptions |
| [TypeScript SDK](api/typescript-sdk.md) | `MemoryClient`, types, WebSocket |
| [REST API](api/rest-api.md) | HTTP endpoints, WebSocket events |

## Architecture

| Document | Description |
|----------|-------------|
| [Overview](architecture/overview.md) | High-level system design |
| [Subsystems](architecture/subsystems.md) | Deep dive into each component |

## Features

### Search Pipeline

12-phase hybrid search: FTS5 BM25 + vector + ColBERT + RRF + cross-encoder + temporal decay + neural forget + KG boost. Each phase independently isolated.

### Knowledge Graph

Entity extraction with Jaccard fuzzy matching, temporal edges with valid_at/invalid_at, contradiction detection, supersession chains.

### Multi-Agent Sync

CRDT field-level LWWES for conflict-free replication. CQRS write journal for lock-free writes. Saga transactions for crash consistency.

### Neural Forget

Surprise-based retention formula considering query relevance, access patterns, recency, and importance.

### Integrations

Native support for LangChain, CrewAI, OKF (Open Knowledge Format), MCP, REST API, WebSocket.

## System Requirements

- Python 3.11+
- SQLite with FTS5
- ~100MB disk space for DB + indexes
- No cloud connection required

## License

Apache 2.0
