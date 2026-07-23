Six sibling modules split two concerns — index building and result re-ranking — each exposing a small public API that mutates or reads SQLite tables in the same database used by the rest of the search pipeline.

Indexers (write paths):
- `chunk_index.py` defines QW5 topic-aware chunking (`_qw5_chunk_content`) plus an idempotent FTS5-backed `memory_chunks` table with triggers; consumed by other indexers.
- `colbert_index.py` stores per-token float32 BLOBs from ColBERT-v2 into `colbert_tokens`, using `_qw5_chunk_content` to split documents first; provides single-memory and batch variants plus delete/scan helpers.
- `splade_index.py` stores SPLADE-v3 sparse `(vocab_id, weight)` pairs into `splade_tokens` and implements the maxsim SQL-side scorer (`splade_search`).
All three follow the same shape: `_ensure_*_schema(conn)` + `index_memory_*` / `index_memory_*_batch` + `delete_memory_*` + `get_indexed_memory_ids`.

Rerankers (read-only score blending, all mutate `r[6] = final_score` on the top-k head then append the untouched tail):
- `rerankers.py` is the largest file. It contains the weak hand-rolled CE (IDF+bigram), the character-n-gram late-interaction proxy, a chunk-level ms-marco-MiniLM cross-encoder stage with its own retry/eviction cache and p80 pre-filter, and PR1.2's unified `_apply_combined_ce_rerank` that collapses the old sequential weak+chunk stages into one deterministic `r[6]` write. Deep reranker (Qwen3/BGE-m3 via `infra.reranker`) is gated behind `MEMORY_CE_DEEP` / config flags and falls back to weak/chunk.
- `answer_rerank.py` extracts a keyword-overlap best window snippet per candidate, scores it against the query (CE with fallback), blends into `r[6]`, and persists results in `answer_rerank_cache` for cron pre-computation.
- `colbert_rerank.py` performs true ColBERT MaxSim at query time by loading query token vectors, batch-fetching doc token BLOBs from `colbert_tokens`, and blending the normalized sum-of-max-cosine score into `r[6]`; guarded by adaptive depth gates (≤30 candidates, ≥3 query tokens).

Dependency direction: indexers depend on `search.chunk_index` and `infra.*_encoder`; rerankers depend only on `search.config.get_search_config()` and `infra._lazy_imports.get_config()` (no circular import with `search_pipeline`). All heavy model imports are lazy inside functions to keep cold-start fast.