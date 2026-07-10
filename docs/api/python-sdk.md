# Python SDK API Reference

## Overview

The Python SDK (`agentic-memory` package) provides a programmatic interface to all memory operations without going through MCP. Use it for scripting, batch operations, custom integrations, and any scenario where you want to call memory functions directly from Python code. The SDK wraps the same internal pipeline as the MCP tools.

## Quick Reference

| Method | Description | Returns |
|--------|-------------|---------|
| `MemoryClient(db_path, user_id)` | Create a new client | Client instance |
| `.save(content, category, ...)` | Save a memory | Note ID (str) |
| `.search(query, limit, ...)` | Search memories | `SearchResults` |
| `.get(note_id)` | Get a single memory | `MemoryResult` or None |
| `.delete(note_id, hard)` | Soft/hard delete | bool |
| `.restore(note_id)` | Restore from soft-delete | bool |
| `.list(limit, offset, category)` | List recent memories | list of `MemoryResult` |
| `.stats()` | System statistics | `Stats` |
| `.clear()` | Clear SDK-created memories | int |
| `AgentMemory(agent_id, db_path)` | Agent-scoped client | Client instance |

## Installation

```bash
pip install agentic-memory
```

## Quick Start

```python
from agentic_memory import MemoryClient

mc = MemoryClient()
mc.save("User prefers dark mode")
results = mc.search("dark mode")
```

## Classes

### MemoryClient

Core SDK client wrapping the full agentic-memory system.

```python
class MemoryClient(
    db_path: str | Path | None = None,
    user_id: str = "default",
)
```

**Parameters:**
- `db_path` — Path to memory.db (default: auto-detect from config)
- `user_id` — User identifier for multi-user setups

#### Methods

##### save()

Save a memory and return its note ID.

```python
def save(
    content: str,
    category: str = "sdk",
    tags: list[str] | None = None,
    pinned: bool = False,
    is_global: bool = False,
    importance: int = 3,
    title_slug: str = "",
) -> str
```

**Parameters:**
- `content` — Text content to store (required)
- `category` — Subdirectory: lessons, projects, decisions, preferences, sessions, sdk
- `tags` — Optional list of tag strings
- `pinned` — Boost in recall results
- `is_global` — Store at global config level
- `importance` — 1-5 ranking weight (default: 3)
- `title_slug` — Explicit slug (auto-generated if empty)

**Returns:** Note ID string

**Example:**
```python
note_id = mc.save(
    "User prefers dark mode and vim keybindings",
    category="preferences",
    tags=["ui", "editor"],
    importance=4,
)
```

##### search()

Search memories by semantic relevance.

```python
def search(
    query: str,
    limit: int = 5,
    rerank: bool = True,
    boost_pinned: bool = True,
    recency_weight: float = 0.1,
    include_global: bool = True,
    include_facts: bool = True,
    fact_limit: int = 5,
    synthesize: bool = False,
    max_synthesis_sentences: int = 5,
    tags: list[str] | None = None,
) -> SearchResults
```

**Parameters:**
- `query` — Search query (required)
- `limit` — Max results (default: 5)
- `rerank` — Enable cross-encoder reranking (default: True)
- `boost_pinned` — Boost pinned memories (default: True)
- `recency_weight` — Weight for recent memories (default: 0.1)
- `include_global` — Include global memories (default: True)
- `include_facts` — Include KG facts (default: True)
- `fact_limit` — Max facts to return (default: 5)
- `synthesize` — Generate answer synthesis (default: False)
- `tags` — Filter by tags

**Returns:** `SearchResults` with typed `MemoryResult` objects

**Example:**
```python
results = mc.search(
    "What does the user prefer?",
    limit=10,
    rerank=True,
    synthesize=True,
)
print(results.synthesis)
for r in results:
    print(f"[{r.score:.2f}] {r.content}")
```

##### delete()

Soft-delete (or hard-purge) a memory.

```python
def delete(note_id: str, hard: bool = False) -> bool
```

**Parameters:**
- `note_id` — Memory ID to delete
- `hard` — If True, permanently delete (default: False)

**Returns:** True if successful

##### restore()

Restore a soft-deleted memory.

```python
def restore(note_id: str) -> bool
```

##### get()

Retrieve a single memory by ID.

```python
def get(note_id: str) -> MemoryResult | None
```

##### list()

List recent memories.

```python
def list(
    limit: int = 50,
    offset: int = 0,
    category: str = "",
) -> list[MemoryResult]
```

##### stats()

Return memory system statistics.

```python
def stats() -> Stats
```

**Returns:** `Stats` with memories, vector_keys, chunks, facts, entities, relations counts

##### clear()

Clear all SDK-created memories.

```python
def clear() -> int
```

**Returns:** Count of memories cleared

---

### AgentMemory

Agent-scoped memory client for multi-agent systems.

```python
class AgentMemory(
    agent_id: str,
    db_path: str | Path | None = None,
)
```

**Parameters:**
- `agent_id` — Unique agent identifier
- `db_path` — Optional database path

Same methods as `MemoryClient`, plus:
- `agent_id` property — The agent's identifier
- All saves are scoped to this agent
- Searches only return this agent's memories (unless `include_global=True`)

---

## Data Models

### MemoryResult

```python
@dataclass
class MemoryResult:
    id: str
    content: str
    score: float = 0.0
    tags: list[str] = field(default_factory=list)
    category: str = ""
    created_at: str = ""
    pinned: bool = False
    importance: int = 3
    metadata: dict[str, Any] = field(default_factory=dict)
```

### SearchResults

```python
@dataclass
class SearchResults:
    results: list[MemoryResult] = field(default_factory=list)
    total: int = 0
    synthesis: str = ""
    query: str = ""
```

### Stats

```python
@dataclass
class Stats:
    memories: int = 0
    vector_keys: int = 0
    chunks: int = 0
    facts: int = 0
    entities: int = 0
    relations: int = 0
```

### Entity

```python
@dataclass
class Entity:
    id: str
    name: str
    entity_type: str
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
```

### Relation

```python
@dataclass
class Relation:
    id: str
    source: str
    target: str
    relation_type: str
    weight: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)
```

### Fact

```python
@dataclass
class Fact:
    id: str
    subject: str
    predicate: str
    obj: str
    confidence: float = 1.0
    source_memory: str = ""
    event_time: str = ""
    event_time_granularity: str = ""
    valid_at: str = ""
    invalid_at: str = ""
    superseded_by: str = ""
    supersedes: str = ""
    contradiction_score: float = 0.0
    locked: bool = False
```

---

## Configuration

The Python SDK reads the same configuration as the MCP server — environment variables and `memory.toml`. Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_DB_PATH` | `./memory.db` | Database location |
| `MEMORY_MULTI_AGENT` | `true` | Enable cross-agent sharing |
| `MEMORY_KNOWLEDGE_GRAPH` | `true` | Enable KG in search results |

When creating a `MemoryClient(db_path="/custom/path.db")`, the explicit path overrides all config.

## Troubleshooting

### Symptom: `ConnectionError` on client creation

**Cause**: The database path doesn't exist or is locked by another process.

**Fix**: Verify the DB path exists and no other process holds a write lock. Check `MEMORY_DB_PATH` is set correctly.

### Symptom: `NotFoundError` when calling `.get()` or `.delete()`

**Cause**: The note ID doesn't exist or has been hard-deleted.

**Fix**: Use `.search()` to find the correct ID, or `.list()` to browse available memories.

### Symptom: `CircuitBreakerOpen` on save

**Cause**: The auto-save circuit breaker is open after too many failures.

**Fix**: Check `memory_maintenance(operation="circuit_breaker_status")` for details. The breaker auto-resets after 5 minutes.

## Related

- [TypeScript SDK](typescript-sdk.md) — TypeScript equivalent
- [REST API](rest-api.md) — HTTP interface
- [MCP Tools Reference](../reference/mcp-tools.md) — MCP tool equivalents

## Exceptions

```python
from agentic_memory.exceptions import (
    AgenticMemoryError,      # Base exception
    ConnectionError,         # DB connection failed
    NotFoundError,           # Memory not found
    ValidationError,         # Invalid input
    IntegrityError,          # Data integrity issue
    MaintenanceError,        # Maintenance operation failed
    SyncError,               # Sync operation failed
    PermissionError,         # Access denied
    CircuitBreakerOpen,      # Circuit breaker active
    ConfigError,             # Configuration error
)
```
