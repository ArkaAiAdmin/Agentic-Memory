# How to Debug Search Issues

## Goal

Diagnose and fix search quality issues — missing results, low relevance, or stale indexes in the memory system.

## Prerequisites

- [ ] Access to the memory database (`memory.db`)
- [ ] Python 3.10+ with `sqlite3` module
- [ ] Familiarity with FTS5 and basic SQL

## Steps

### 1. Check Index Health

```bash
# Run integrity check
agentic-memory-integrity

# Check FTS5 index
python -c "
import sqlite3
conn = sqlite3.connect('memory.db')
count = conn.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
print(f'Chunks indexed: {count}')
if count == 0:
    print('WARNING: No chunks indexed. Run agentic-memory-rebuild')
"
```

### 2. Rebuild the Index

If the index is stale or corrupted:

```bash
# Rebuild from markdown files
agentic-memory-rebuild

# Or rebuild everything
agentic-memory-backfill
```

### 3. Check Search Pipeline

### Test FTS5 directly

```bash
python -c "
import sqlite3
conn = sqlite3.connect('memory.db')
results = conn.execute(
    \"SELECT id, content, bm25(chunks) as rank FROM chunks WHERE chunks MATCH 'your-query' ORDER BY rank LIMIT 5\"
).fetchall()
for r in results:
    print(f'[{r[2]:.2f}] {r[0]}: {r[1][:80]}...')
"
```

### Test semantic search

```bash
python -c "
import sqlite3
conn = sqlite3.connect('memory.db')
# Check if embeddings exist
count = conn.execute('SELECT COUNT(*) FROM memory_embeddings').fetchone()[0]
print(f'Embeddings: {count}')
if count == 0:
    print('WARNING: No embeddings. Enable MEMORY_EMBEDDINGS=1 and rebuild.')
"
```

### Test knowledge graph

```bash
python -c "
import sqlite3
conn = sqlite3.connect('memory.db')
entities = conn.execute('SELECT COUNT(*) FROM kg_entities').fetchone()[0]
facts = conn.execute('SELECT COUNT(*) FROM kg_facts').fetchone()[0]
print(f'Entities: {entities}, Facts: {facts}')
"
```

### 4. Compare Search Modes

### Quick search (FTS5 only)

```python
from agentic_memory import search_memories

results = search_memories("your query", limit=5)
for r in results:
    print(f'[{r["score"]:.2f}] {r["content"][:80]}')
```

### Semantic search

```python
results = search_memories("your query", limit=5, include_embeddings=True)
```

### Knowledge graph search

```python
from agentic_memory import search_graph

entities = search_graph("your query", limit=10)
```

### 5. Check Query Quality

### Too specific

```
# Bad: too many constraints
"SQLite WAL mode concurrent reads performance optimization"

# Good: broader
"SQLite concurrency"
```

### Too vague

```
# Bad: too broad
"database"

# Good: more specific
"SQLite WAL mode"
```

### Synonyms

FTS5 doesn't understand synonyms. Use semantic search:

```
# FTS5 won't match "car" with "automobile"
# Semantic search will
```

### 6. Check for Common Issues

### Stale index

```bash
# Check last rebuild time
python -c "
import sqlite3, os
conn = sqlite3.connect('memory.db')
mtime = os.path.getmtime('memory.db')
from datetime import datetime
print(f'Last modified: {datetime.fromtimestamp(mtime)}')
"
```

### Missing FTS5

```bash
python -c "
import sqlite3
conn = sqlite3.connect(':memory:')
try:
    conn.execute('CREATE VIRTUAL TABLE test USING fts5(content)')
    print('FTS5: OK')
except Exception as e:
    print(f'FTS5: NOT AVAILABLE ({e})')
"
```

### Wrong memory directory

```bash
# Check where memories are stored
python -c "
from agentic_memory import resolve_active_memory_dir
print(f'Active dir: {resolve_active_memory_dir()}')
"
```

## Troubleshooting

### Search still broken after rebuild

**Cause**: The FTS5 index is corrupted or the embeddings are out of sync.
**Fix**: Run `agentic-memory-backfill` instead of `agentic-memory-rebuild` to rebuild from scratch.

### FTS5 not available

**Cause**: The Python `sqlite3` module was compiled without FTS5 support.
**Fix**: Install a Python build with FTS5: `pip install pysqlite3-binary` or use the system Python on macOS (which ships FTS5).

### semantic search returns empty results

**Cause**: No embeddings exist in `memory_embeddings` table.
**Fix**: Enable `MEMORY_EMBEDDINGS=1` and rebuild the index.

## Verification

After applying any fix, confirm the search produces expected results:

```bash
python -c "
from agentic_memory import search_memories
results = search_memories('your query', limit=5)
print(f'Found {len(results)} results')
for r in results:
    print(f'  [{r[\"score\"]:.2f}] {r[\"content\"][:80]}')
"
```

Expected output: Relevant results with scores above 0.5 (BM25) or above 0.7 (semantic).

### 7. Advanced Debugging

### Enable verbose logging

```bash
export MEMORY_LOG_LEVEL=debug
python -c "
import logging
logging.basicConfig(level=logging.DEBUG)
from agentic_memory import search_memories
results = search_memories('test')
"
```

### Check score distribution

```bash
python -c "
import sqlite3
conn = sqlite3.connect('memory.db')
scores = conn.execute('SELECT bm25(chunks) FROM chunks LIMIT 100').fetchall()
scores = [s[0] for s in scores]
print(f'Score range: {min(scores):.2f} to {max(scores):.2f}')
print(f'Average: {sum(scores)/len(scores):.2f}')
"
```

## Related

- [Search Pipeline](../concepts/search-pipeline.md) — How search works
- [Configuration](../reference/configuration.md) — All search-related settings
- [Knowledge Graph](../concepts/knowledge-graph.md) — How entities are used in search
- [Performance Benchmarks](performance-benchmarks.md) — Expected latency and throughput
