# Knowledge Graph

The knowledge graph captures **entities and relationships** extracted from memories, enabling structured queries like "show me all memories related to PostgreSQL" or "what decisions involved the auth system?"

## How It Works

### 1. Entity Extraction (NER)

When a memory is saved, the system runs **regex-based named entity recognition** to extract entities:

```python
# Patterns for common entity types
ENTITY_PATTERNS = {
    "technology": r"\b(Python|SQLite|PostgreSQL|Docker|FastAPI|React)\b",
    "concept":    r"\b(authentication|caching|migration|deployment)\b",
    "file":       r"\b([a-zA-Z0-9_/.-]+\.(py|js|ts|md|yaml|json))\b",
    "command":    r"\b(git|npm|pip|docker|kubectl)\s+\S+",
    "url":        r"https?://[^\s]+",
    "email":      r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
}
```

Each extracted entity becomes a node in the graph:

```
(memory) "lessons/sqlite-wal-mode"
    ├── mentions → (entity) "SQLite"
    ├── mentions → (entity) "WAL mode"
    └── mentions → (entity) "PRAGMA journal_mode"
```

### 2. Relationship Extraction

The system also extracts **Subject-Predicate-Object (SPO) triples** from memory content:

```python
# Regex-based SPO extraction
TRIPLE_PATTERNS = [
    r"(\w[\w\s]*?)\s+(?:is|are|was|were)\s+(.+)",     # "X is Y"
    r"(\w[\w\s]*?)\s+(?:uses?|uses?)\s+(.+)",          # "X uses Y"
    r"(\w[\w\s]*?)\s+(?:requires?|requires?)\s+(.+)",  # "X requires Y"
]
```

Example extracted triples:

```
(SQLite, requires, WAL mode for concurrency)
(WAL mode, enables, concurrent reads)
(FastAPI, uses, async/await)
```

### 3. Graph Storage

Entities and relationships are stored in SQLite:

```sql
-- Entities
CREATE TABLE kg_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    entity_type TEXT,
    mentions INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(name, entity_type)
);

-- Relationships (edges)
CREATE TABLE knowledge_graph_edges (
    source_name TEXT,
    source_type TEXT,
    target_name TEXT,
    target_type TEXT,
    relation TEXT,
    weight REAL DEFAULT 1.0,
    memory_ids TEXT,  -- JSON array of evidence memories
    UNIQUE(source_name, source_type, target_name, target_type, relation)
);

-- Facts (SPO triples)
CREATE TABLE kg_facts (
    id TEXT PRIMARY KEY,
    subject TEXT,
    predicate TEXT,
    object TEXT,
    confidence REAL,
    source_memory_id TEXT,
    created_at TEXT
);
```

### 4. Deduplication

Entities are deduplicated using two strategies:

**Exact deduplication:**
```python
# Group by (name, entity_type) and merge
SELECT name, entity_type, COUNT(*) as mentions
FROM kg_entities
GROUP BY name, entity_type
HAVING COUNT(*) > 1;
```

**Semantic deduplication:**
```python
# Use model2vec embeddings for fuzzy matching
embeddings = model.encode([e["name"] for e in candidates])
# Cosine similarity threshold (default: 0.92)
for i, j in itertools.combinations(range(len(candidates)), 2):
    if cosine_similarity(embeddings[i], embeddings[j]) > threshold:
        merge_entities(candidates[i], candidates[j])
```

## Graph Queries

### Find entities by name

```python
from agentic_memory import search_graph

entities = search_graph("SQLite", limit=10)
# Returns: [{name: "SQLite", type: "technology", mentions: 5, memories: [...]}]
```

### Traverse relationships

```python
# Find entities connected to "SQLite" within 2 hops
entities = search_graph("SQLite", max_hops=2)
# Hop 1: SQLite → WAL mode, FTS5, database
# Hop 2: WAL mode → concurrent reads, FTS5 → BM25 ranking
```

### Search by entity type

```python
# Find all technology entities
from agentic_memory import search_graph

tech_entities = search_graph("*", entity_type="technology", limit=50)
```

## When KG Helps

The knowledge graph is most valuable when:

- **Cross-referencing** — "Show everything related to Docker"
- **Relationship discovery** — "What's connected to the auth system?"
- **Entity tracking** — "How many times have I mentioned PostgreSQL?"
- **Contradiction detection** — "Do I have conflicting facts about Redis?"

## When KG Doesn't Help

The knowledge graph has limitations:

- **Regex-based NER** — Misses domain-specific entities
- **No deep semantics** — Can't understand "the database" = "SQLite" without explicit mention
- **Sparse graphs** — Small memory stores may have few connections
- **O(n²) dedup** — Semantic dedup scales poorly with many entities

## Configuration

| Variable | Default | Effect |
|----------|---------|--------|
| `MEMORY_KNOWLEDGE_GRAPH` | `0` | Enable KG extraction |
| `--semantic` flag | off | Enable semantic dedup |
| `--threshold` | `0.92` | Similarity threshold for semantic dedup |

## Further Reading

- [Search Pipeline](search-pipeline.md) — How KG integrates with search
- [Design Decisions](../explanation/design-decisions.md) — Why regex-based NER
- [Extend Entity Types](../how-to/custom-entity-types.md) — Add custom patterns
