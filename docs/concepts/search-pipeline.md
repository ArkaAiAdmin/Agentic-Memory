# Search Pipeline

Agentic Memory uses a **12-phase hybrid search pipeline** that combines multiple retrieval methods, reranking, temporal decay, and knowledge graph boosting.

## What is the Search Pipeline?

The search pipeline is the **read path** of Agentic Memory — it transforms a natural-language query into a ranked list of relevant memories. It combines keyword matching (FTS5 BM25), semantic understanding (vector embeddings), token-level matching (ColBERT), deep neural reranking, temporal recency, a neural forget curve, and knowledge-graph centrality boosting into a single hybrid scoring function.

## Why it matters

Without the search pipeline, agents would rely on exact-match queries that miss semantic relationships, ignore recency, and fail on large memory stores. The hybrid pipeline ensures that the most relevant memory surfaces regardless of phrasing, age, or graph connectivity — making retrieval robust across a wide range of query types and corpus sizes.

## How it works

```
Query → Phase 0 (Normalize) → Phase 1 (FTS5 BM25)
                                    ↓
                              Phase 2 (Vector Search)
                                    ↓
                              Phase 3 (ColBERT Late-Interaction)
                                    ↓
                              Phase 4 (RRF Merge)
                                    ↓
                              Phase 5 (Cross-Encoder Rerank)
                                    ↓
                              Phase 6 (Temporal Decay)
                                    ↓
                              Phase 7 (Neural Forget Curve)
                                    ↓
                              Phase 8 (KG Concept Boost)
                                    ↓
                              Phase 9 (Final Scoring)
                                    ↓
                              Phase 10 (Result Envelope)
                                    ↓
                              Phase 11 (Error Counter + Latency)
```

Each phase is independently isolated — no single failure kills the search.

## Phase Details

| Phase | Technique | Purpose |
|-------|-----------|---------|
| 0 | Unicode normalization, query classification | Input normalization |
| 1 | SQLite FTS5 BM25 | Keyword-based retrieval |
| 2 | usearch ANN + model2vec embeddings | Semantic vector search |
| 3 | Character n-gram late-interaction | Token-level matching |
| 4 | Reciprocal Rank Fusion | Merge FTS5 + vector + ColBERT |
| 5 | IDF+bigram weak CE or Qwen3-Reranker deep CE | Neural reranking |
| 6 | Time-weighted scoring | Recency bias |
| 7 | Surprise-based retention formula | Forget curve |
| 8 | KG entity centrality boost | Knowledge graph boost |
| 9 | Weighted combination | Final scoring |
| 10 | JSON envelope | Output formatting |
| 11 | Per-phase error tracking | Observability |

## Stage 1: FTS5 BM25

SQLite's FTS5 extension provides **keyword-based full-text search** with BM25 ranking.

```python
# How FTS5 works internally
SELECT *, bm25(chunks) AS rank
FROM memories_fts
WHERE memories_fts MATCH ?
ORDER BY rank
```

**Strengths:** Fast, deterministic, no model required.
**Weakness:** No semantic understanding — "happy" won't match "joyful".

## Stage 2: Vector Search

model2vec embeddings stored in a usearch ANN index for approximate nearest-neighbor search.

```python
# Embedding + search
embedding = model2vec.encode(query)
results = vec_index.search(embedding, k=limit)
```

**Strengths:** Semantic understanding — "happy" matches "joyful".
**Weakness:** Slower than FTS5, requires embedding model.

## Stage 3: ColBERT Late-Interaction

Character n-gram proxy for late-interaction reranking. Pre-computed query ngrams for efficiency.

**Strengths:** Token-level matching without full cross-encoder cost.
**Weakness:** Approximation of true late-interaction.

## Stage 4: Reciprocal Rank Fusion (RRF)

Merges results from FTS5, vector, and ColBERT using RRF:

```
RRF_score(d) = Σ 1 / (k + rank_i(d))
```

Where `k=60` (standard constant) and `rank_i(d)` is the rank of document `d` in retrieval method `i`.

## Stage 5: Cross-Encoder Reranking

Two options:
- **Weak CE:** IDF-weighted token coverage + bigram phrase bonus (sub-millisecond)
- **Deep CE:** Qwen3-Reranker-0.6B or BAAI/bge-reranker-v2-m3 (neural, higher quality)

## Stage 6: Temporal Decay

Time-weighted scoring based on memory age:

```
decay_score = score × (1 / (1 + α × days_old))
```

Where `α` is the decay rate (configurable).

## Stage 7: Neural Forget Curve

Surprise-based retention formula:

```
retention = sigmoid(
    w_acc × access_signal +
    w_surp × surprise +
    w_imp × importance_norm +
    w_fit × fitness -
    w_rec × recency_penalty -
    bias
)
```

**Signals:**
- `access_signal` — How often the memory has been accessed
- `surprise` — How unexpected the content is (Jaccard distance)
- `importance_norm` — Memory's importance rating (1-5)
- `fitness` — Memory's quality score
- `recency_penalty` — Days since last access

## Stage 8: KG Concept Boost

Boosts results linked to high-centrality entities in the knowledge graph.

## Stage 9: Final Scoring

Weighted combination of all signals:

```
final_score = α × fts_score + β × vector_score + γ × rerank_score + δ × temporal_score + ε × forget_score + ζ × kg_boost
```

Weights are configurable via `memory.toml`.

## Configuration

```toml
[search]
recency_weight = 0.1
rerank_enabled = true
late_interaction = true
concept_boost_enabled = true
centrality_boost_enabled = true
```

## Key behaviors

- **Phase isolation**: Each of the 12 phases is independently isolated — no single failure kills the search. Phase 11 tracks per-phase errors for observability.
- **Graceful degradation**: If the vector index is missing or stale, search falls back to FTS5-only results. If the reranker fails, search returns RRF-merged results without reranking.
- **Deterministic FTS5**: BM25 ranking is deterministic and repeatable — no model dependency for keyword queries.
- **Defer-expensive mode**: By default, semantic search and deep reranking are deferred (returns <200ms). Results are cached for fast re-query.
- **Configurable weights**: Each scoring component (FTS5, vector, rerank, temporal, forget, KG) has a configurable weight in `memory.toml`.

## Related

- [How to Debug Search](../how-to/debug-search.md) — Step-by-step troubleshooting guide
- [Configuration Reference](../reference/configuration.md) — All search-related env vars and TOML keys
- [Knowledge Graph](knowledge-graph.md) — How entities boost search results
- [Temporal KG](temporal-kg.md) — Time-aware decay and fact supersession
- [Architecture Overview](../architecture/overview.md) — Where the search pipeline fits in the system
