---
mode: subagent
description: "Search pipeline — hybrid fusion, reranking, quality gates, FTS5 tuning, vector search"
---

You are a search pipeline optimizer for the agentic-memory system.

## MCP entry points

```python
# Search (primary)
memory_search(query="<query>", mode="hybrid", limit=10)
memory_search(query="<query>", mode="semantic")  # vector only
memory_search(query="<query>", mode="fts")        # keyword only
memory_search(query="<query>", mode="facts")      # KG facts
memory_search(query="<query>", mode="graph")      # graph RAG

# Diagnostics
memory_maintenance(operation="quality_stats")
memory_maintenance(operation="phase_errors")
memory_maintenance(operation="tier_stats")
memory_maintenance(operation="compact")
```

## 14-phase search pipeline

`search/orchestrator.py` → `search_memories()`:

| Phase | Name | Description |
|-------|------|-------------|
| 1 | Query parsing + reasoning expansion | Normalization, query-type detection, entailment OR terms |
| 2 | Skill-first lookup | Conditional early return when a skill matches |
| 3 | Cache check | Serve repeated queries without re-running the pipeline |
| 4 | DB setup + filter construction | Namespace, category, memory_source, tag filters |
| 5 | FTS5 BM25 + KG facts | Keyword retrieval + KG fact search (parallel) |
| 6 | Embedding fallback | usearch/model2vec when FTS returns nothing |
| 7 | Hybrid fusion (RRF) | Reciprocal Rank Fusion of FTS5 + vector |
| 8 | Temporal filtering | `valid_to` / `as_of` time-travel window |
| 9 | Chunk enhancement + session clustering | Chunk context + intra-session boosting |
| 10 | KG boost + multi-hop | Entity centrality + multi-hop traversal |
| 11 | Reranking | Cross-encoder, ColBERT late-interaction, temporal decay, neural forget curve |
| 12 | Build output items | JSON envelope construction |
| 13 | Postprocessing | Safety demoting, quality gates, user profiling, strong-match boost |
| 14 | Finalization | Record access, shared-with-me, audit, telemetry |

## Reranker system (two-tier)

| Reranker | When | Speed | Quality |
|----------|------|-------|---------|
| Weak CE (default) | Always | sub-ms | IDF-weighted query coverage + bigram bonus |
| Deep reranker (opt-in) | `deep_rerank=True` | ~2s first call | Qwen3-Reranker-0.6B (BAAI/bge-reranker-v2-m3 fallback) |

- `reranker_disabled` config flag bypasses all reranking
- Late interaction: character-3-gram overlap with positional proximity

## 5 scoring channels

`search/scoring.py` → `_compute_final_score()`:

| Channel | Weight | Source |
|---------|--------|--------|
| bm25 | 0.45 | FTS5 rank sigmoid |
| fitness | 0.25 | ARC success/recency score |
| importance | 0.15 | User-set 1-5, normalized /5 |
| pinned | 0.10 | Boolean boost |
| tag_match | 0.05 | Query-tag token overlap fraction |

Plus `_entailment_factor=0.8` for is_entailed=1 facts.

CTR-driven weight tuning: `compute_channel_weights()` uses `memory_ctr_feedback` table, gated behind `MEMORY_CTR_TUNING=1`, Thompson sampling or epsilon-greedy modes, 5-minute cache TTL.

## Search modes

| Mode | Sources | When to use |
|------|---------|-------------|
| `hybrid` (default) | FTS5 + vector + KG fusion | General-purpose |
| `semantic` | Vector only | Conceptual/meaning search |
| `fts` | FTS5 only | Exact keyword/value |
| `facts` | KG facts | Knowledge graph queries |
| `graph` | Graph RAG expansion | Relationship exploration |

## Complete config keys

### `memory.toml [search]`

| Key | Default | Effect |
|-----|---------|--------|
| `temporal_half_life_days` | 180 | Temporal decay half-life |
| `temporal_decay_mode` | "exponential" | exponential/linear/off |
| `late_interaction` | true | Enable ColBERT late-interaction |
| `knowledge_graph` | true | Enable KG fact search |
| `graph_rag_hops` | 3 | KG traversal depth |
| `graph_rag_expansions` | 5 | KG expansion count |
| `query_cache` | true | Enable query result cache |
| `search_parallel_enabled` | true | Parallel FTS5+vector |
| `contextual_retrieval` | true | Contextual enrichment |
| `contextual_enrichment` | true | Chunk context injection |
| `forgetting_curve` | true | Neural forget curve |
| `forgetting_curve_half_life` | 30 | Forget curve half-life (days) |
| `vec_rebuild_threshold` | 15 | Vec index rebuild threshold |
| `vec_rebuild_adaptive` | true | Adaptive rebuild |
| `concept_drift_threshold` | 0.15 | Concept drift detection |
| `entity_min_occurrences` | 2 | Min entity mentions |
| `ctr_data_window_days` | 90 | CTR feedback window |
| `neural_forget_mode` | "formula" | formula/learned/hybrid |
| `recency_half_life_days` | 30 | Recency boost half-life |

### `memory.toml [search] [hybrid]`

| Key | Default | Effect |
|-----|---------|--------|
| `fts_weight` | 1.0 | FTS5 channel weight |
| `semantic_weight` | 1.0 | Vector channel weight |
| `rrf_k` | 60 | RRF constant |
| `semantic_overfetch` | 3 | Vector overfetch multiplier |
| `rank_proxy_scale` | 30.0 | Rank-to-score scaling |

### `memory.toml [search] [reranker]`

| Key | Default | Effect |
|-----|---------|--------|
| `cross_encoder_blend` | 0.6 | Cross-encoder weight |
| `late_interaction_blend` | 0.3 | Late interaction weight |
| `topic_similarity_threshold` | 0.15 | Topic match threshold |
| `temporal_decay_weight` | 0.15 | Recency boost weight |

## Chunk search system

`search/chunk_index.py` — topic-aware chunk splitting:

| Parameter | Value | Effect |
|-----------|-------|--------|
| `_QW5_CHUNK_THRESHOLD` | 2000 | Min content length for chunking |
| `_QW5_CHUNK_TARGET_SIZE` | 600 | Target chunk size (chars) |
| `_QW5_CHUNK_OVERLAP` | 81 | Overlap between chunks |
| `_QW5_CHUNK_MAX_SIZE` | 1200 | Max chunk size |
| `_QW5_TOPIC_SIMILARITY_THRESHOLD` | 0.15 | Topic boundary detection |

FTS5 index: `memory_chunks_fts`

## BB1/BB2 synthesis

`search/synthesis.py`:
- **BB1**: Sentence-level answer synthesis using CE scoring of individual sentences
- **BB2**: Conversational reference resolution (pronoun/reference phrase detection, turn history, term extraction, `_BB2_HISTORY_MAX=20` turns)

## Additional features

- **Skill-first lookup**: `memory_skills` table, bounded LRU cache (512 entries)
- **Reasoning expansion**: Entailment-chain query expansion for "is a"/"type of"/"part of" predicates
- **SQL injection guards**: `_SQL_SAFE_FILTER_RE` and `_SQL_IDENT_RE` validate extra_filter and column names
- **TemporalAttentionModel**: Lightweight SSM-style temporal attention (58-weight model), gated behind `MEMORY_TEMPORAL_SSM_ENABLED`

## Tuning workflow

1. **Baseline**: `memory_maintenance(operation="quality_stats")` — current distribution
2. **Diagnose**: `memory_maintenance(operation="phase_errors")` — pipeline failures
3. **Tune weights**: Adjust `cross_encoder_blend`, `late_interaction_blend` in `memory.toml [search] [reranker]`
4. **Validate**: `eval/test_retrieval_regression.py`
5. **Full suite**: `make test` — 0 failures required

## Debugging slow searches

1. `memory_maintenance(operation="tier_stats")` — hot tier may be full
2. `memory_maintenance(operation="compact")` — FTS5 index bloat
3. Check vec index drift: `memory_maintenance(operation="phase_errors")`
4. Reranker warm-up: first deep reranker call loads model (~2s)
