# Specialized Indexes (Colbert & SPLADE)

The **Specialized Indexes** module implements advanced multi-vector late interaction (ColBERT) and learned sparse representation (SPLADE) retrieval mechanisms.

## Retrieval Architectures

Agentic Memory combines standard dense vector embeddings and BM25 full-text search with specialized neural retrieval indexes for edge cases requiring fine-grained token alignment or high-recall term expansion:

- **ColBERT (Late Interaction)**: Preserves multi-vector token embeddings (`N x D`) per document. Computes max-similarity (`MaxSim`) operator over token matrices during query execution for precise term matching.
- **SPLADE (Sparse Expansion)**: Expands text into sparse vocabulary-space distributions (`V`), providing neural term weighting and synonym expansion while utilizing inverted index search structures.

## Implementation Details

| Component | File Path | Responsibilities |
| :--- | :--- | :--- |
| **ColBERT Indexer** | [search/colbert_index.py](file://search/colbert_index.py) | Token matrix storage, MaxSim score computation, and index updates |
| **SPLADE Indexer** | [search/splade_index.py](file://search/splade_index.py) | Sparse vector generation, inverted index management |
| **Backfill Worker** | [scripts/backfill_colbert_splade.py](file://scripts/backfill_colbert_splade.py) | Asynchronous backfill script for pre-existing memory collections |

## Integration with 14-Phase Search Pipeline

Specialized index scores feed directly into the Phase 5 (Result Fusion) stage alongside dense vector search, BM25, and Knowledge Graph scores using Reciprocal Rank Fusion (RRF):

$$RRF\_Score(d) = \sum_{m \in M} \frac{1}{k + r_m(d)}$$

Where $r_m(d)$ is the rank of document $d$ in retrieval phase $m$.
