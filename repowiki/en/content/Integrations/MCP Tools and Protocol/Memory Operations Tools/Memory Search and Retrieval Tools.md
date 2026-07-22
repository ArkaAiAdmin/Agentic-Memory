# Memory Search and Retrieval Tools

The **Memory Search and Retrieval Tools** provide high-performance hybrid semantic and full-text search operations via the Model Context Protocol (MCP).

## Overview & Tool Surface

The search surface exposes fine-grained retrieval primitives designed for fast context insertion in agent loops:

- **`memory_search`**: The primary hybrid search tool combining dense vector similarity, BM25 keyword matching, Reciprocal Rank Fusion (RRF), and optional LightGBM reranking across 24 CORE search phases.
- **`recall_memory`**: High-recall memory retrieval tool optimized for long-context agent prompts and conversational memory window reconstruction.
- **`memory_search_by_tag`**: Categorical memory filtering tool supporting tag sets, category filters, and temporal window constraints.

## 24 CORE Search Retrieval Pipeline

```mermaid
graph LR
    Query["Query String"] --> Parse["Phase 1-4: Query Parser & Expansion"]
    Parse --> Vector["Phase 5-8: Dense Vector & ColBERT Search"]
    Parse --> BM25["Phase 9-11: FTS5 BM25 & SPLADE Search"]
    Parse --> KG["Phase 12-14: KG Traversal & Contradiction Engine"]
    Vector --> RRF["Phase 15-18: RRF Fusion & Temporal Solver"]
    BM25 --> RRF
    KG --> RRF
    RRF --> LTR["Phase 19-22: LTR & Answer Reranker"]
    LTR --> Telemetry["Phase 23-24: Synthesis & Telemetry"]
```

## Key Query Parameters & Code Invariants

```json
{
  "query": "vector indexing and saga write pattern",
  "limit": 10,
  "include_global": true,
  "tags": ["architecture", "database"],
  "min_score": 0.35
}
```

- **`include_global=True`**: Extends search scope beyond the active `agent_id` to include shared system-wide knowledge.
- **Hybrid RRF Ranking**: Combines vector cosine similarity and FTS5 BM25 scores without requiring manual weight tuning.
- **Rate-Limiting Protection**: Search endpoints enforce token-bucket rate limits (`get_default_limiter()`) to prevent resource exhaustion during agent loops.
