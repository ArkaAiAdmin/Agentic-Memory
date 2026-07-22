# Search Index Maintenance

The **Search Index Maintenance** tools manage vector index rebuilding, FTS5 full-text index synchronization, embedding recomputation, and index quality checks.

## Maintenance Subsystem Overview

To maintain low-latency retrieval across large memory collections, Agentic Memory provides dedicated maintenance MCP tools and cron routines:

- **Index Rebuild Engine ([mcp_rebuild.py](file://mcp_rebuild.py))**: Reconstructs vector indexes (Usearch / HNSW) and FTS tables from canonical SQLite records.
- **Embedding Recomputation Engine ([cron_embedding_recompute.py](file://cron_embedding_recompute.py))**: Detects model version shifts or dimension mismatches and re-embeds stored memories incrementally.
- **Quality Assurance Router ([mcp_quality.py](file://mcp_quality.py))**: Evaluates index sparsity, vector drift, and orphan memory nodes.

## Maintenance Operations

### 1. FTS5 Index Rebuild
If full-text search indexes become out of sync with SQLite `memories` table:
```python
from mcp_rebuild import rebuild_fts_index

status = rebuild_fts_index(db_path="memory.db")
print(f"Rebuilt FTS index: {status['reindexed_count']} entries updated.")
```

### 2. Vector Index Re-indexing
```python
from rebuild_vec_index import rebuild_vector_index

res = rebuild_vector_index(deep_check=True)
assert res["status"] == "healthy"
```

## Key Invariants

- **Atomic Index Swap**: Index rebuilding uses temp files (`.usearch.tmp`) and swaps atomically upon completion to avoid blocking read queries.
- **Incremental Lock**: Operations acquire `_acquire_incremental_lock()` to ensure no concurrent save operations mutate the index mid-rebuild.
