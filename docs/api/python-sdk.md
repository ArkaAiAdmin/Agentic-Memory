# Python SDK API Reference

## Overview

The Python SDK (`agentic-memory` package) provides a programmatic interface to all memory operations without going through MCP. Use it for scripting, batch operations, custom integrations, and any scenario where you want to call memory functions directly from Python code. The SDK wraps the same internal pipeline as the MCP tools.

## Installation

```bash
pip install agentic-memory
```

## Quick Start

```python
from agentic_memory import MemoryClient

mc = MemoryClient()
note_id = mc.save("User prefers dark mode")
results = mc.search("dark mode")
for r in results:
    print(f"[{r.score:.2f}] {r.content}")
```

---

## Quick Reference

| Method | Description | Returns |
|--------|-------------|---------|
| `MemoryClient(db_path, user_id)` | Create a new client | Client instance |
| `.save(content, ...)` | Save a memory | `str` (note ID) |
| `.search(query, ...)` | Semantic search | `SearchResults` |
| `.get(note_id)` | Get single memory | `MemoryResult \| None` |
| `.list(limit, offset, category)` | List memories | `list[MemoryResult]` |
| `.delete(note_id, hard)` | Soft/hard delete | `bool` |
| `.restore(note_id)` | Restore soft-deleted | `bool` |
| `.clear()` | Clear SDK memories | `int` |
| `.stats()` | System statistics | `Stats` |
| `.rebuild(scope)` | Rebuild FTS5 index | `str` |
| `.scan_injection(content)` | Scan for injections | `dict` |
| `.check_contradictions(content)` | Check contradictions | `list[dict]` |
| `.get_user_profile()` | User preference profile | `dict` |
| `.record_access(note_id)` | Record note access | `None` |
| `.check_integrity(deep)` | DB health check | `IntegrityReport` |
| `.audit()` | SRMA audit metrics | `dict` |
| `.search_facts(query, limit)` | Search KG facts | `list[Fact]` |
| `.list_facts(limit, offset)` | List recent facts | `list[Fact]` |
| `.quality_filter(query, limit)` | Quality-filtered search | `list[dict]` |
| `.quality_stats()` | Quality gate stats | `dict` |
| `.summarize(note_id)` | Summarize a note | `str` |
| `.adaptive_retention(dry_run)` | Retention scoring | `str` |
| `AgentMemory(agent_id, ...)` | Agent-scoped client | Client instance |
| `TemporalKG(db_path)` | Temporal KG client | Client instance |

---

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

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `db_path` | `str \| Path \| None` | `None` | Path to memory.db. Auto-detects from config if None. |
| `user_id` | `str` | `"default"` | User identifier for multi-user setups. |

**Raises:**
- `ConnectionError` — DB path doesn't exist or is locked
- `ConfigError` — Configuration resolution failed

**Example:**
```python
from agentic_memory import MemoryClient

# Default: auto-detect DB path
mc = MemoryClient()

# Custom DB path
mc = MemoryClient(db_path="/custom/path/memory.db")

# Context manager (auto-cleanup)
with MemoryClient() as mc:
    mc.save("test")
```

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

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `content` | `str` | (required) | Text content to store. Must be non-empty. |
| `category` | `str` | `"sdk"` | Subdirectory: `lessons`, `projects`, `decisions`, `preferences`, `sessions`, `sdk` |
| `tags` | `list[str] \| None` | `None` | Optional list of tag strings |
| `pinned` | `bool` | `False` | Boost in recall results |
| `is_global` | `bool` | `False` | Store at global config level (shared across agents) |
| `importance` | `int` | `3` | 1-5 ranking weight (1=low, 5=critical) |
| `title_slug` | `str` | `""` | Explicit slug (auto-generated if empty) |

**Returns:** `str` — The note ID (e.g. `"preferences/user-prefers-dark-mode"`)

**Raises:**
- `ValidationError` — Content is empty or importance out of range

**Example:**
```python
note_id = mc.save(
    "User prefers dark mode and vim keybindings",
    category="preferences",
    tags=["ui", "editor"],
    importance=4,
)
# Returns: "preferences/user-prefers-dark-mode-and-vim-keybindings"
```

##### search()

Search memories by semantic and keyword relevance.

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

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | `str` | (required) | Natural-language search query |
| `limit` | `int` | `5` | Maximum results to return |
| `rerank` | `bool` | `True` | Enable cross-encoder reranking |
| `boost_pinned` | `bool` | `True` | Boost pinned memories |
| `recency_weight` | `float` | `0.1` | Recency bias weight (0.0 = disabled) |
| `include_global` | `bool` | `True` | Include global-scope memories |
| `include_facts` | `bool` | `True` | Include KG facts in results |
| `fact_limit` | `int` | `5` | Max KG facts to return |
| `synthesize` | `bool` | `False` | Generate LLM synthesis (adds latency) |
| `max_synthesis_sentences` | `int` | `5` | Max sentences in synthesis output |
| `tags` | `list[str] \| None` | `None` | Filter by tags |

**Returns:** `SearchResults` — Iterable container with typed `MemoryResult` objects.

**Example:**
```python
results = mc.search(
    "What does the user prefer?",
    limit=10,
    rerank=True,
    synthesize=True,
)

# Access synthesis
print(results.synthesis)

# Iterate results
for r in results:
    print(f"[{r.score:.2f}] {r.content}")

# Filter by metadata
for r in results:
    if r.importance >= 4:
        print(f"Important: {r.content}")
```

##### get()

Retrieve a single memory by note ID.

```python
def get(note_id: str) -> MemoryResult | None
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `note_id` | `str` | (required) | Memory note ID to retrieve |

**Returns:** `MemoryResult | None` — The memory dataclass, or None if not found.

**Example:**
```python
note = mc.get("preferences/user-prefers-dark-mode")
if note:
    print(f"Content: {note.content}")
    print(f"Tags: {note.tags}")
else:
    print("Note not found")
```

##### list()

List recent memories, optionally filtered by category.

```python
def list(
    limit: int = 50,
    offset: int = 0,
    category: str = "",
) -> list[MemoryResult]
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | `int` | `50` | Maximum results per page |
| `offset` | `int` | `0` | Skip first N results (pagination) |
| `category` | `str` | `""` | Filter by category (empty = all) |

**Returns:** `list[MemoryResult]` — Ordered newest first.

**Example:**
```python
recent = mc.list(limit=10)
for m in recent:
    print(f"{m.created_at}: {m.content[:50]}")

# Paginate
page1 = mc.list(limit=20, offset=0)
page2 = mc.list(limit=20, offset=20)

# Filter by category
prefs = mc.list(category="preferences", limit=5)
```

##### delete()

Delete a memory by note ID. Default is soft-delete (recoverable for 30 days).

```python
def delete(note_id: str, hard: bool = False) -> bool
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `note_id` | `str` | (required) | Memory note ID to delete |
| `hard` | `bool` | `False` | Permanently delete (not recoverable) |

**Returns:** `bool` — True if successful.

**Example:**
```python
mc.delete("notes/my-note")           # soft-delete (recoverable)
mc.delete("notes/old-note", hard=True)  # permanent
```

##### restore()

Restore a soft-deleted memory.

```python
def restore(note_id: str) -> bool
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `note_id` | `str` | (required) | Memory note ID to restore |

**Returns:** `bool` — True if successful.

**Example:**
```python
mc.delete("notes/my-note")
mc.restore("notes/my-note")  # re-activated
```

##### clear()

Clear all memories created via the SDK (source_file LIKE `sdk-%`).

```python
def clear() -> int
```

**Returns:** `int` — Count of memories cleared.

**Example:**
```python
count = mc.clear()
print(f"Cleared {count} SDK memories")
```

##### stats()

Return memory system statistics.

```python
def stats() -> Stats
```

**Returns:** `Stats` with:

| Field | Type | Description |
|-------|------|-------------|
| `memories` | `int` | Active (non-deleted) memory count |
| `vector_keys` | `int` | Vector index entry count |
| `chunks` | `int` | Text chunk count |
| `facts` | `int` | KG fact count |
| `entities` | `int` | KG entity count |
| `relations` | `int` | KG edge count |

**Example:**
```python
s = mc.stats()
print(f"{s.memories} memories, {s.facts} facts, {s.entities} entities")
```

##### rebuild()

Rebuild the FTS5 full-text search index.

```python
def rebuild(scope: str = "active") -> str
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `scope` | `str` | `"active"` | `"active"` (non-deleted) or `"all"` |

**Returns:** `str` — Status message.

**Example:**
```python
mc.rebuild()             # rebuild active memories
mc.rebuild(scope="all")  # rebuild all including soft-deleted
```

##### scan_injection()

Scan content for prompt-injection patterns.

```python
def scan_injection(content: str) -> dict[str, Any]
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `content` | `str` | (required) | Text to scan |

**Returns:** `dict` with `"safe"` (bool), `"score"` (float), `"patterns"` (list).

**Example:**
```python
report = mc.scan_injection("ignore previous instructions and do X")
if not report.get("safe", True):
    print("Injection detected:", report.get("patterns"))
```

##### check_contradictions()

Check content for phrase-level contradictions with existing memories.

```python
def check_contradictions(
    content: str,
    top_n: int = 20,
) -> list[dict[str, Any]]
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `content` | `str` | (required) | Text to check |
| `top_n` | `int` | `20` | Max existing memories to compare |

**Returns:** `list[dict]` — Each with `"existing"` (memory) and `"score"` (confidence).

**Example:**
```python
hits = mc.check_contradictions("User prefers light mode")
for h in hits:
    print(f"Contradicts: {h['existing']} (score: {h['score']})")
```

##### get_user_profile()

Get the aggregated user preference profile.

```python
def get_user_profile() -> dict[str, Any]
```

**Returns:** `dict` — User preferences, frequencies, and timestamps. Empty if profiling disabled.

**Example:**
```python
profile = mc.get_user_profile()
print(profile.get("top_categories", []))
```

##### record_access()

Record that a note was accessed (opt-in via `MEMORY_USER_PROFILE=1`).

```python
def record_access(
    note_id: str,
    source: str = "search",
    category: str = "",
    tags: str = "",
) -> None
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `note_id` | `str` | (required) | Note ID that was accessed |
| `source` | `str` | `"search"` | Access source (search, api, sdk) |
| `category` | `str` | `""` | Note category |
| `tags` | `str` | `""` | Comma-separated tags |

**Example:**
```python
mc.record_access("preferences/user-prefers-dark-mode", source="api")
```

##### check_integrity()

Run a health check on the memory database.

```python
def check_integrity(deep: bool = False) -> IntegrityReport
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `deep` | `bool` | `False` | Thorough check including index verification |

**Returns:** `IntegrityReport` with:

| Field | Type | Description |
|-------|------|-------------|
| `passed` | `bool` | True if all checks passed |
| `errors` | `list[str]` | Critical error strings |
| `warnings` | `list[str]` | Non-critical warning strings |
| `stats` | `dict` | Diagnostic metrics |

**Example:**
```python
report = mc.check_integrity()
if not report.passed:
    for err in report.errors:
        print(f"ERROR: {err}")
```

##### audit()

Audit memory system health (SRMA metrics).

```python
def audit() -> dict[str, Any]
```

**Returns:** `dict` — Search/Retention/Maintenance/Audit metrics.

**Example:**
```python
metrics = mc.audit()
print(f"Save rate: {metrics.get('save_rate', 'N/A')}")
```

##### search_facts()

Search extracted knowledge graph facts (SPO triples).

```python
def search_facts(query: str, limit: int = 10) -> list[Fact]
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | `str` | (required) | Natural-language query |
| `limit` | `int` | `10` | Max results |

**Returns:** `list[Fact]` — Subject-Predicate-Object triples with temporal metadata.

**Example:**
```python
facts = mc.search_facts("user preferences")
for f in facts:
    print(f"{f.subject} {f.predicate} {f.obj} (conf: {f.confidence})")
```

##### list_facts()

List recent knowledge graph facts.

```python
def list_facts(limit: int = 50, offset: int = 0) -> list[Fact]
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | `int` | `50` | Max results |
| `offset` | `int` | `0` | Pagination offset |

**Returns:** `list[Fact]`

**Example:**
```python
facts = mc.list_facts(limit=10)
print(f"Listed {len(facts)} facts")
```

##### quality_filter()

Search with quality gates (validation + deduplication).

```python
def quality_filter(query: str, limit: int = 50) -> list[dict[str, Any]]
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | `str` | (required) | Search query |
| `limit` | `int` | `50` | Max results |

**Returns:** `list[dict]` — Quality-filtered results.

**Example:**
```python
clean = mc.quality_filter("deployment workflow")
print(f"Quality results: {len(clean)}")
```

##### quality_stats()

Return quality gate statistics.

```python
def quality_stats() -> dict[str, Any]
```

**Returns:** `dict` — Quality gate metrics.

**Example:**
```python
qs = mc.quality_stats()
print(f"Dedup rate: {qs.get('dedup_rate', 'N/A')}")
```

##### summarize()

Summarize a specific note using extractive TF-IDF.

```python
def summarize(note_id: str) -> str
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `note_id` | `str` | (required) | Note ID to summarize |

**Returns:** `str` — Summary text.

**Raises:** `NotFoundError` — Note does not exist.

**Example:**
```python
summary = mc.summarize("notes/my-long-note")
print(summary)
```

##### adaptive_retention()

Compute adaptive half-lives and neural forget curve scores.

```python
def adaptive_retention(dry_run: bool = False) -> str
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `dry_run` | `bool` | `False` | Preview without writing to DB |

**Returns:** `str` — Retention metrics summary.

**Example:**
```python
mc.adaptive_retention(dry_run=True)  # preview only
mc.adaptive_retention()              # apply scores
```

---

### AgentMemory

Agent-scoped memory with namespace isolation for multi-agent systems.

```python
class AgentMemory(
    agent_id: str,
    display_name: str = "",
    parent_agent: str | None = None,
    db_path: str | Path | None = None,
)
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `agent_id` | `str` | (required) | Unique agent identifier |
| `display_name` | `str` | `""` | Human-readable name (defaults to agent_id) |
| `parent_agent` | `str \| None` | `None` | Parent agent ID for hierarchy |
| `db_path` | `str \| Path \| None` | `None` | Database path override |

**Properties:**

| Property | Type | Description |
|----------|------|-------------|
| `client` | `MemoryClient` | Underlying client sharing the same DB |
| `info` | `AgentInfo` | Agent context metadata |

**Methods:**

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `save()` | `(content, category="agents", tags=None)` | `str` | Save agent-scoped memory |
| `search()` | `(query, limit=10)` | `SearchResults` | Search this agent's memories |
| `list()` | `(limit=50)` | `list[MemoryResult]` | List agent's memories |
| `clear()` | `()` | `int` | Clear all agent memories |
| `list_agents()` | `()` (static) | `list[AgentInfo]` | List all registered agents |
| `reset()` | `()` | `None` | Clear agent context (revert to default) |

**Example:**
```python
from agentic_memory import AgentMemory

# Agent-scoped operations
am = AgentMemory(agent_id="coder-1", display_name="Coder Agent")
am.save("Frontend uses React with TypeScript")
results = am.search("frontend")
print(f"Found {results.total} results")

# List all agents
for agent in AgentMemory.list_agents():
    print(f"{agent.agent_id}: {agent.display_name}")

# Context manager (auto-reset on exit)
with AgentMemory(agent_id="reviewer") as am:
    am.save("Review checklist updated")

# Hierarchy
am = AgentMemory(
    agent_id="child-1",
    parent_agent="parent-agent",
    display_name="Child Agent",
)
```

---

### TemporalKG

Temporal KG operations — time-aware fact queries, contradiction tracking, and supersession chains.

```python
class TemporalKG(
    db_path: str | Path | None = None,
)
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `db_path` | `str \| Path \| None` | `None` | Database path override |

**Methods:**

##### search()

Search facts with temporal awareness (only currently valid facts).

```python
def search(query: str, limit: int = 10) -> list[Fact]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | `str` | (required) | Substring match against subject/predicate/object |
| `limit` | `int` | `10` | Max results (1-1000) |

**Returns:** `list[Fact]` — Currently valid facts matching the query.

##### contradictions()

List fact supersession/contradiction events.

```python
def contradictions(
    since_ts: float | None = None,
    until_ts: float | None = None,
    reason: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `since_ts` | `float \| None` | `None` | Min transaction_time (epoch) |
| `until_ts` | `float \| None` | `None` | Max transaction_time (epoch) |
| `reason` | `str \| None` | `None` | Filter by reason (e.g. `"contradicted"`) |
| `limit` | `int` | `50` | Max rows (1-500) |
| `offset` | `int` | `0` | Pagination offset |

**Returns:** `list[dict]` — Each with `"old"`, `"new"`, `"reason"`, `"contradiction_score"`, `"transaction_time"`.

##### query_facts_at_time()

Facts valid at a given epoch timestamp.

```python
def query_facts_at_time(
    timestamp: float,
    query: str | None = None,
    limit: int = 50,
) -> list[Fact]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `timestamp` | `float` | (required) | Epoch seconds to query at |
| `query` | `str \| None` | `None` | Optional substring filter |
| `limit` | `int` | `50` | Max results |

**Returns:** `list[Fact]` — Facts valid at the given timestamp.

##### query_changed_since()

Facts that changed (inserted or invalidated) since a timestamp.

```python
def query_changed_since(
    timestamp: float,
    limit: int = 100,
) -> list[Fact]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `timestamp` | `float` | (required) | Epoch seconds lower bound |
| `limit` | `int` | `100` | Max results |

**Returns:** `list[Fact]` — Changed facts, most-recent first.

##### query_supersession_chain()

Walk the full supersession chain for a fact (oldest first).

```python
def query_supersession_chain(fact_id: str | int) -> list[Fact]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `fact_id` | `str \| int` | (required) | Any fact ID in the chain |

**Returns:** `list[Fact]` — Full history in chronological order. Empty if fact doesn't exist.

##### invalidate_fact()

Manually invalidate a fact (mark as no longer valid).

```python
def invalidate_fact(
    fact_id: str | int,
    reason: str = "manual",
) -> bool
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `fact_id` | `str \| int` | (required) | Fact ID to invalidate |
| `reason` | `str` | `"manual"` | Invalidation reason |

**Returns:** `bool` — True if invalidated. False if not found, locked, or already invalidated.

**Raises:** `NotFoundError` — Fact does not exist.

**Example:**
```python
from agentic_memory import TemporalKG

tk = TemporalKG()

# Search currently valid facts
facts = tk.search("user prefers")
for f in facts:
    print(f"{f.subject} {f.predicate} {f.obj}")

# Check contradictions
events = tk.contradictions(limit=5)
for e in events:
    print(f"Contradiction: {e.get('reason')}")

# Historical query
import time
old_facts = tk.query_facts_at_time(time.time() - 86400)  # 24h ago

# Walk supersession chain
chain = tk.query_supersession_chain(fact_id=42)
for f in chain:
    print(f"{f.id}: {f.subject} {f.predicate} {f.obj}")

# Invalidate
tk.invalidate_fact(fact_id=42, reason="outdated")
```

---

## Data Models

### MemoryResult

```python
@dataclass
class MemoryResult:
    id: str                          # Note ID
    content: str                     # Text content
    score: float = 0.0              # Relevance score (0-1)
    tags: list[str] = []            # Tag strings
    category: str = ""              # Category (lessons, preferences, etc.)
    created_at: str = ""            # ISO 8601 timestamp
    pinned: bool = False            # Pinned flag
    importance: int = 3             # 1-5 importance weight
    metadata: dict[str, Any] = {}   # Extra metadata
```

### SearchResults

```python
@dataclass
class SearchResults:
    results: list[MemoryResult] = []  # Ranked results
    total: int = 0                     # Total result count
    synthesis: str = ""                # LLM synthesis (if requested)
    query: str = ""                    # Original query

    # Iterable: for r in search_results: ...
    def __len__(self) -> int
    def __iter__(self)
```

### Stats

```python
@dataclass
class Stats:
    memories: int = 0      # Active memory count
    vector_keys: int = 0   # Vector index entries
    chunks: int = 0        # Text chunks
    facts: int = 0         # KG facts
    entities: int = 0      # KG entities
    relations: int = 0     # KG edges
```

### Fact

```python
@dataclass
class Fact:
    id: str                                        # Fact ID
    subject: str                                   # Subject entity
    predicate: str                                 # Relationship
    obj: str                                       # Object entity
    confidence: float = 1.0                        # Confidence (0-1)
    source_memory: str = ""                        # Source note ID
    event_time: str = ""                           # Event timestamp
    event_time_granularity: str = ""               # Granularity (day, month, etc.)
    valid_at: str = ""                             # Valid-from timestamp
    invalid_at: str = ""                           # Valid-until timestamp
    superseded_by: str = ""                        # ID of superseding fact
    supersedes: str = ""                           # ID of fact this supersedes
    contradiction_score: float = 0.0               # Contradiction confidence
    locked: bool = False                           # Manual lock flag
```

### IntegrityReport

```python
@dataclass
class IntegrityReport:
    passed: bool = True              # All checks passed
    errors: list[str] = []           # Critical errors
    warnings: list[str] = []         # Non-critical warnings
    stats: dict[str, Any] = {}       # Diagnostic metrics
```

### Entity

```python
@dataclass
class Entity:
    id: str                              # Entity ID
    name: str                            # Entity name
    entity_type: str                     # Type (person, concept, etc.)
    description: str = ""                # Description
    metadata: dict[str, Any] = {}        # Extra metadata
```

### Relation

```python
@dataclass
class Relation:
    id: str                              # Relation ID
    source: str                          # Source entity ID
    target: str                          # Target entity ID
    relation_type: str                   # Relationship type
    weight: float = 1.0                 # Edge weight
    metadata: dict[str, Any] = {}        # Extra metadata
```

### AgentInfo

```python
@dataclass
class AgentInfo:
    agent_id: str                        # Agent identifier
    display_name: str = ""               # Human-readable name
    parent_agent: str = ""               # Parent agent ID
    namespace: str = ""                  # Namespace prefix
```

---

## Exceptions

```python
from agentic_memory.exceptions import (
    AgenticMemoryError,      # Base exception for all SDK errors
    ConnectionError,         # DB connection failed or pool exhausted
    NotFoundError,           # Note, entity, or fact does not exist
    ValidationError,         # Invalid input (bad category, missing content)
    IntegrityError,          # Database integrity check failed
    MaintenanceError,        # Maintenance operation failed (rebuild, compact)
    SyncError,               # Multi-agent sync or CRDT operation failed
    PermissionError,         # Operation not allowed in current context
    CircuitBreakerOpen,      # Circuit breaker active (too many failures)
    ConfigError,             # Configuration resolution failed
)
```

**Example:**
```python
from agentic_memory import MemoryClient
from agentic_memory.exceptions import ValidationError, NotFoundError

mc = MemoryClient()

try:
    mc.save("")
except ValidationError as e:
    print(f"Invalid: {e}")

try:
    mc.get("nonexistent/id")
except NotFoundError as e:
    print(f"Not found: {e}")
```

---

## Configuration

The Python SDK reads the same configuration as the MCP server — environment variables and `memory.toml`. Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_DB_PATH` | `./memory.db` | Database location |
| `MEMORY_MULTI_AGENT` | `true` | Enable cross-agent sharing |
| `MEMORY_KNOWLEDGE_GRAPH` | `true` | Enable KG in search results |
| `MEMORY_USER_PROFILE` | `0` | Enable user profiling (set to `1`) |
| `MEMORY_WRITE_JOURNAL_ENABLED` | `false` | Enable CQRS write journal |

When creating a `MemoryClient(db_path="/custom/path.db")`, the explicit path overrides all config.

---

## Troubleshooting

### `ConnectionError` on client creation

**Cause**: The database path doesn't exist or is locked by another process.

**Fix**: Verify the DB path exists and no other process holds a write lock. Check `MEMORY_DB_PATH`.

### `NotFoundError` when calling `.get()` or `.delete()`

**Cause**: The note ID doesn't exist or has been hard-deleted.

**Fix**: Use `.search()` to find the correct ID, or `.list()` to browse available memories.

### `CircuitBreakerOpen` on save

**Cause**: The auto-save circuit breaker is open after too many failures.

**Fix**: Check `mc.admin.circuit_breaker_status()`. The breaker auto-resets after 5 minutes.

### Empty search results when data exists

**Cause**: FTS5 or vector index out of sync.

**Fix**: `mc.rebuild()` to regenerate the index.

---

## Related

- [TypeScript SDK](typescript-sdk.md) — TypeScript equivalent
- [REST API](rest-api.md) — HTTP interface
- [MCP Tools Reference](../reference/mcp-tools.md) — MCP tool equivalents
