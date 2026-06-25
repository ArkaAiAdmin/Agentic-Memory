# Search Pipeline

Agentic Memory uses a **hybrid search pipeline** that combines three retrieval methods, then ranks results with a learned ranker.

## Overview

```
Query
  │
  ├──▶ FTS5 BM25 (keyword match)
  ├──▶ Vector search (semantic match)
  ├──▶ Knowledge graph (entity lookup)
  │
  ▼
┌──────────────┐
│   Ranker     │
│ (score merge)│
└──────┬───────┘
       │
       ▼
  Results (top-k)
```

## Stage 1: FTS5 BM25

SQLite's FTS5 extension provides **keyword-based full-text search** with BM25 ranking.

```python
# How FTS5 works internally
SELECT *, bm25(chunks) AS rank
FROM chunks
WHERE chunks MATCH 'sqlite concurrency'
ORDER BY rank
LIMIT 20;
```

**Strengths:**
- Fast (microseconds for small-to-medium corpora)
- Handles exact matches well
- No model loading required
- Works offline with zero dependencies

**Weaknesses:**
- No semantic understanding ("car" ≠ "automobile")
- No contextual ranking
- Sensitive to exact word forms

## Stage 2: Vector Search (Optional)

When enabled (`MEMORY_EMBEDDINGS=1`), the system uses `model2vec` embeddings for **semantic similarity**.

```python
# How vector search works
query_embedding = model.encode("sqlite concurrency patterns")
# L2-normalized: cosine similarity = dot product
similarities = np.dot(query_embedding, stored_embeddings.T)
top_indices = np.argsort(similarities)[-k:]
```

**Strengths:**
- Understands meaning ("car" ≈ "automobile")
- Captures conceptual similarity
- Works across languages

**Weaknesses:**
- Requires model loading (~50MB)
- Slower than FTS5 (milliseconds vs microseconds)
- Less precise for exact keyword matches

## Stage 3: Knowledge Graph (Optional)

When enabled (`MEMORY_KNOWLEDGE_GRAPH=1`), the system searches the **entity-relation graph**.

```python
# How KG search works
entities = search_graph("SQLite", max_hops=2)
# Returns: entity → relations → related entities → memories
```

**Strengths:**
- Captures relationships between concepts
- Enables "hop" queries (find related entities)
- Provides structured context

**Weaknesses:**
- Depends on NER quality (regex-based)
- Sparse for small memory stores
- No semantic ranking

## Stage 4: Ranker

The ranker merges results from all three stages into a **single ranked list**.

### Scoring

Each result gets a score from 0.0 to 1.0:

```
final_score = (bm25_score × bm25_weight) +
              (vector_score × vector_weight) +
              (kg_score × kg_weight) +
              (recency_bonus × recency_weight)
```

Default weights (configurable):

| Source | Weight | Rationale |
|--------|--------|-----------|
| FTS5 BM25 | 0.4 | Keyword relevance is primary |
| Vector | 0.3 | Semantic match is secondary |
| KG | 0.2 | Relationship context is tertiary |
| Recency | 0.1 | Recent memories get a small boost |

### Reranking (Optional)

When the cross-encoder reranker is available, results are reranked for higher precision:

```python
# Cross-encoder reranking
reranked = cross_encoder.rank(
    query=query,
    passages=[r["content"] for r in results],
    top_k=10
)
```

The cross-encoder evaluates each (query, passage) pair directly, producing more accurate relevance scores than the separate-encode-then-compare approach.

## Pipeline Modes

### Quick Search (Default)

```
FTS5 → top-20 → Ranker → top-10
```

No vector search, no reranking. Fastest option.

### Semantic Search

```
FTS5 → top-20 ─┐
                ├──→ Ranker → top-10
Vector → top-20 ┘
```

Adds semantic similarity. Requires `model2vec` installed.

### Full Search

```
FTS5 → top-20 ─┐
                ├──→ Ranker → top-20 → Reranker → top-10
Vector → top-20 ┘
KG → top-10 ─────┘
```

All three sources + reranking. Slowest but most accurate.

## Configuration

| Variable | Default | Effect |
|----------|---------|--------|
| `MEMORY_EMBEDDINGS` | `0` | Enable vector search |
| `MEMORY_KNOWLEDGE_GRAPH` | `0` | Enable KG search |
| `rerank=true` | `false` | Enable cross-encoder reranking |
| `deep_rerank=true` | `false` | Use jina-reranker-v3 (slower, better) |

## Tuning Search Quality

### If results are too noisy

- Increase `min_score` threshold
- Use more specific queries
- Enable reranking for precision

### If relevant results are missing

- Try broader queries
- Enable vector search for semantic matching
- Enable KG for relationship-based retrieval

### If search is too slow

- Disable vector search (use FTS5 only)
- Reduce `limit` parameter
- Rebuild the FTS5 index: `python rebuild_index.py`

## Further Reading

- [Why Markdown](why-markdown.md) — Why the index is derived
- [Knowledge Graph](knowledge-graph.md) — Entity extraction details
- [Debug Search](../how-to/debug-search.md) — Troubleshooting guide
