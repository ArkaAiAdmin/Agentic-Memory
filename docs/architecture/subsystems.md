# Subsystem Deep Dive

## Search Pipeline (search/)

### 12-Phase Pipeline

| Phase | File | LOC | Purpose |
|-------|------|-----|---------|
| 0 | `query_parser.py` | 400+ | Input normalization, query type detection |
| 1 | `orchestrator.py` | 2825 | FTS5 BM25 retrieval |
| 2 | `orchestrator.py` | — | Vector (usearch) retrieval |
| 3 | `rerankers.py` | 560 | ColBERT late-interaction |
| 4 | `orchestrator.py` | — | Reciprocal Rank Fusion |
| 5 | `rerankers.py` | — | Cross-encoder reranking |
| 6 | `scoring.py` | 866 | Temporal decay |
| 7 | `orchestrator.py` | — | Neural forget curve |
| 8 | `scoring.py` | — | KG concept/centrality boost |
| 9 | `orchestrator.py` | — | Final scoring |
| 10 | `orchestrator.py` | — | Result envelope |
| 11 | `orchestrator.py` | — | Error counter + latency |

### Chunk Indexing (chunk_index.py)

- Topic-aware boundary detection using Jaccard similarity
- Overlapping chunks (600 char target, 81 char overlap, 1200 char max)
- Separate FTS5 table for chunk-level search
- Keyword extraction for topic comparison

### Query Expansion (query_parser.py)

- Synonym/abbreviation expansion (QW2)
- Typo/synonym correction (did-you-mean)
- Query type classification (QW3)
- Graph RAG expansion via KG entity linking

### Synthesis (synthesis.py)

- BB1: Sentence-level answer synthesis
- BB2: Conversational reference resolution
- Turn history tracking for context-aware synthesis

## Save Pipeline (save/)

### Saga Pattern (saga.py)

```python
with Saga(name="save_memory", steps=[SagaStep("upsert", do_fn, undo_fn)]) as saga:
    result = saga.results[0]
```

**Crash Consistency:** If any step fails, the saga undo restores the previous state.

### Write Path (pipeline.py)

1. Validate input (injection scanning, content checks)
2. Normalize content (unicode, whitespace)
3. Begin saga
4. Upsert memory row
5. Update FTS5 index
6. Update vector index
7. Write markdown file
8. Extract KG entities/edges
9. Extract facts
10. Update backlinks
11. Enqueue background tasks
12. Commit saga

### Backlinks (backlinks.py)

- Bidirectional link tracking between memories
- Wiki-link style `[[note_id]]` syntax
- Auto-generated from content analysis

## Infrastructure (infra/)

### Database Pool (db.py)

- Per-DB-path connection pooling
- WAL mode for concurrent reads
- Background revalidation loop
- Stale connection detection

### CQRS Write Journal (write_journal.py)

- Lock-free multi-agent writes
- Separate journal.db for write-ahead logging
- Background reconciliation daemon
- Crash-consistent materialization

### Vector Store (vector_store.py)

- Abstraction over usearch/faiss/hnswlib
- Pure-Python NumPy fallback
- Add/remove/search interface

### Embedding Search (embedding_search.py)

- model2vec for lightweight embeddings
- usearch ANN index
- Incremental indexing
- Model change detection

### Reranker (reranker.py)

- Weak CE: IDF + bigram (sub-millisecond)
- Deep CE: Qwen3-Reranker-0.6B or BAAI/bge-reranker-v2-m3
- Lazy-loaded singleton
- MPS safety on Apple Silicon

### API Server (api_server.py)

- Threaded HTTP server
- WebSocket event streaming (Outbox pattern)
- CORS support
- Rate limiting

## Background (background/)

### Auto-Save Daemon (daemon.py)

- Long-lived process for auto-save
- Filesystem watching (kqueue/inotify/sleep)
- Batch processing (50 entries or 0.5s interval)
- Circuit breaker for failure handling
- Idle exit after 300s

### Background Worker (background_worker.py)

- Task queue processor
- Drain mode for backlog burn-down
- Per-task-type handlers
- Graceful shutdown

### Circuit Breaker (circuit_breaker.py)

- Failure threshold detection
- Auto-recovery after cooldown
- Integration with daemon and worker

## Knowledge Graph (knowledge_graph/)

### Entity Extraction (kg_extract.py)

- Jaccard fuzzy matching for entity deduplication
- Entity type classification
- Mention counting

### KG Schema (kg_schema.py)

- `kg_entities` — Entity nodes with centrality
- `kg_entity_aliases` — Alternate names
- `kg_edges` — Temporal relationships
- `kg_facts` — Structured triples

### KG Search (kg_search.py)

- Entity search by name/type
- Edge traversal
- Centrality-based boosting

## Temporal KG (kg/)

### Contradiction Detection (contradiction_detector.py)

- claim-pair contradiction detection
- usearch ANN for fast similarity
- Temporal supersession chains

### Graph Analytics (graph_analytics.py)

- Betweenness centrality
- Community detection
- Concept drift detection

### Temporal Resolver (temporal_resolver.py)

- Time-aware queries
- Fact supersession
- Event-time extraction

## CRDT (crdt/)

### Field-Level LWWES (crdt_field.py)

- Per-field Last-Writer-Wins Element Set
- Concurrent edits to different fields both win
- Version vector comparison

### Merge (crdt_merge.py)

- Dominance checking (strict)
- Concurrent detection
- Vector merging (pointwise max)

## Fact System (fact/)

### Extraction (fact_extract.py)

- LLM-powered fact extraction (opt-in)
- Rule-based fallback
- Belief status tracking

### Temporal Facts (fact_temporal.py)

- `valid_at` / `invalid_at` temporal validity
- Supersession chain walking
- Contradiction detection

### Fact Schema (fact_schema.py)

- `kg_facts` table with temporal columns
- Entity linking
- Confidence scoring
