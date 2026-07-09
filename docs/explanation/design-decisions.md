# Design Decisions

Why Agentic Memory is built the way it is.

## Why Markdown as Source of Truth?

### The Problem with Database-Only

Most memory systems store everything in a database. This creates:

- **Vendor lock-in** — Proprietary format, migration is painful
- **Opacity** — Need a GUI or SQL to inspect data
- **Fragility** — Corruption means data loss
- **Non-portable** — Can't move between tools easily

### Why Markdown Wins

- **Human-readable** — Open in any editor
- **Version-controllable** — Git diffs are meaningful
- **Portable** — Works with any tool
- **Repairable** — Delete DB, rebuild from markdown
- **AI-friendly** — Agents can read/write directly

### The Trade-off

Markdown is slow for search. SQLite FTS5 is fast. So we use both:

```
Markdown (truth) → SQLite (derived index)
```

If the index breaks, rebuild it. If markdown breaks, you've lost a memory.

## Why SQLite?

### Alternatives Considered

| Option | Pros | Cons |
|--------|------|------|
| **PostgreSQL** | Full ACID, extensions | Heavy dependency, server process |
| **Redis** | Fast, in-memory | No persistence by default, server process |
| **SQLite** | Zero config, single file, ACID | Limited concurrency, no network |
| **DuckDB** | Analytical queries | Not designed for FTS5 |
| **In-memory dict** | Fastest | No persistence |

### Why SQLite Won

1. **Zero dependencies** — No server, no install, no config
2. **Single file** — Easy backup, easy move
3. **ACID transactions** — Crash-safe
4. **WAL mode** — Concurrent reads during writes
5. **FTS5** — Built-in full-text search
6. **Python stdlib** — `import sqlite3` just works

### The Concurrency Limitation

SQLite is single-writer. We handle this with:

- **WAL mode** — Allows concurrent reads
- **`BEGIN IMMEDIATE`** — Serializes writes
- **Connection pooling** — Thread-local connections
- **Depth-based re-entrancy** — Prevents deadlocks

## Why BM25 for Search?

### Alternatives Considered

| Option | Pros | Cons |
|--------|------|------|
| **BM25 (FTS5)** | Fast, zero config, built-in | No semantic understanding |
| **Vector search** | Semantic understanding | Slow, requires model |
| **Hybrid** | Best of both | Complexity |
| **Elasticsearch** | Powerful | Heavy dependency |

### Why BM25 is Primary

1. **Speed** — Microseconds, not milliseconds
2. **Zero config** — Works immediately
3. **Predictable** — Same query, same results
4. **Keyword precision** — Exact matches matter

Vector search is optional because:
- Not everyone needs it
- Model loading is expensive
- BM25 works well for most queries

## Why Regex-Based NER?

### Alternatives Considered

| Option | Pros | Cons |
|--------|------|------|
| **spaCy NER** | High accuracy | Heavy model, slow |
| **LLM extraction** | Contextual understanding | API calls, non-deterministic |
| **Regex patterns** | Fast, deterministic, no deps | Limited accuracy |
| **Rule-based** | Domain-specific | Manual maintenance |

### Why Regex Won

1. **No LLM in write path** — Deterministic, no API keys needed
2. **Speed** — Microsecond extraction
3. **No dependencies** — Works with stdlib only
4. **Domain-specific** — You can add your own patterns

The trade-off is lower accuracy, but for a memory system, recall matters more than precision.

## Why Background Tasks?

### The Problem

Some operations are too slow for synchronous execution:

- Entity deduplication: 100-500ms
- Fact consolidation: 50-200ms
- Contradiction detection: 200-1000ms

Running these on every save would make the system feel sluggish.

### The Solution

Enqueue tasks to a SQLite-backed queue, process them via cron:

```
Save (fast) → Enqueue (fast) → Process (async)
```

### Why Not a Daemon?

- **Simplicity** — No process to manage
- **Reliability** — Cron is battle-tested
- **Resource efficiency** — No idle process
- **Portability** — Works everywhere

## Why Apache 2.0?

### Alternatives Considered

| License | Pros | Cons |
|---------|------|------|
| **MIT** | Simple, permissive | No patent protection |
| **Apache 2.0** | Patent protection, permissive | Longer text |
| **GPL** | Strong copyleft | Limits adoption |
| **AGPL** | Network copyleft | Limits cloud adoption |

### Why Apache 2.0

1. **Patent protection** — Important for AI systems
2. **Permissive** — Can be used commercially
3. **Industry standard** — Well-understood by companies
4. **GitHub friendly** — Recognized by GitHub's license detection

## Why Local-First?

### The Problem with Cloud-First

- **Privacy** — Data leaves your machine
- **Vendor lock-in** — Can't move to another provider
- **Latency** — Network round-trips
- **Availability** — Works offline

### Why Local-First Wins

1. **Privacy** — Data never leaves your machine
2. **Speed** — No network latency
3. **Reliability** — Works offline
4. **Portability** — Move files anywhere
5. **Cost** — No hosting fees

The trade-off is no automatic sync, but that's a feature — you choose what to share.

## Further Reading

- [Comparison](comparison.md) — How we compare to alternatives
- [Architecture](../architecture/overview.md) — Full system design
