# Performance Benchmarks

## Goal

Understand the expected performance characteristics of the agentic-memory system — search latency, write throughput, index rebuild times, and memory footprint — to plan capacity and diagnose slowdowns.

## Prerequisites

- [ ] Agentic Memory installed with all extras
- [ ] Python 3.12 in a virtual environment
- [ ] A populated database (8,000+ memories for representative benchmarks)

Agentic Memory is designed for local-first, sub-100ms search latency on
a typical laptop (Apple Silicon M-series or Intel i7, 16 GB RAM, SQLite
DB under 100 MB). Numbers below are representative measurements on
an M2 MacBook Pro; Intel-class hardware will be 1.3–2× slower.

## Environment

- Python 3.12, venv with all extras installed
- `MEMORY_USE_USEARCH=0` (usearch warm-up adds 2–4 s once)
- DB: ~8 000 memories, ~4 000 embeddings, ~2 500 kg_entities,
  ~3 200 kg_edges, FTS5 indexes up-to-date

## Write path (`save_memory`)

| Metric | p50 | p95 | p99 |
|--------|-----|-----|-----|
| Total (synchronous) | 12 ms | 30 ms | 55 ms |
| DB write only | 3 ms | 8 ms | 15 ms |
| Embedding enqueue (deferred) | <1 ms | 2 ms | 4 ms |
| KG enqueue (deferred) | <1 ms | 1 ms | 3 ms |

Deferred embedding + KG extraction are processed by the background
worker in batches; the MCP `memory_save` call therefore returns in
**< 200 ms regardless of model load time**.

## Search path

| Metric | p50 | p95 | p99 |
|--------|-----|-----|-----|
| BM25-only (`search_memories`) | 4 ms | 9 ms | 18 ms |
| Hybrid (BM25 + usearch) | 8 ms | 16 ms | 30 ms |
| Deep reranker (cross-encoder) | 40 ms | 120 ms | 250 ms |

Notes:
- `deep_rerank=False` (default) returns within the BM25/hybrid times.
- `deep_rerank=True` loads `cross-encoder/ms-marco-MiniLM-L-6-v2` (~90 MB)
  on first call; subsequent calls reuse the model in memory.
- MPS (Apple Silicon) users should set `reranker_disabled = true` in
  `memory.toml` or `MEMORY_RERANKER_DISABLED=1` to avoid PyTorch MPS
  kernel hangs.

## Index operations

| Operation | Time |
|-----------|------|
| FTS5 incremental update (1 row) | < 5 ms |
| FTS5 full rebuild (8 000 rows) | ~1.2 s |
| Vector index rebuild (4 000 vectors, 384-dim) | ~1.3 s |
| Vec drift check (no drift) | < 1 ms |

Vector drift is detected by `background_worker` every ~5 minutes;
rebuild is triggered when `active_vec_keys - memories > threshold`
(default: 5).

## Worker throughput

| Scenario | Throughput |
|----------|-----------|
| Embedding tasks only | ~120 tasks/min |
| KG extraction (3 entities/note) | ~40 tasks/min |
| Mixed (50 % embed, 50 % KG) | ~75 tasks/min |

Per-task timeout: 120 s (configurable via `MEMORY_WORKER_TASK_TIMEOUT_S`).

## Memory footprint

| Component | Approximate size |
|-----------|-----------------|
| SQLite DB (8 000 memories, all indexes) | ~25 MB |
| Embedding model (model2vec, 384-dim) | ~150 MB |
| Cross-encoder model | ~90 MB |
| FTS5 index overhead | ~3 MB |

## Running your own benchmarks

```bash
# Search latency
python scripts/bench_search.py --queries 1000 --runs 5

# Write throughput
python scripts/bench_write.py --notes 500 --runs 5

# Full suite (all benchmarks, ~2 min on M2)
python scripts/bench_all.py
```

These scripts are not yet included in the repo; create them by
wrapping `time.perf_counter()` around the corresponding hot-path
entry points and reporting percentile stats (see `numpy.percentile`).

## Verification

Run the benchmark scripts to confirm the system performs within expected ranges:

```bash
python scripts/bench_search.py --queries 100 --runs 3
```

Expected output: p50 search latency should be under 10ms for BM25 and under 20ms for hybrid search on the reference hardware.

## Troubleshooting

### Search latency is much higher than benchmarks

**Cause**: The database has grown beyond the benchmarked 8,000 memories, or the reranker is enabled (adds 30-250ms).
**Fix**: Disable the deep reranker (`MEMORY_RERANKER_DISABLED=1`) and rebuild the FTS5 index. Check vector index drift with `agentic-memory-integrity`.

### Write latency spikes

**Cause**: A background task (K G extraction, embedding) is blocking the write path.
**Fix**: Ensure `defer_expensive=True` (default) so embedding and KG extraction run asynchronously.

### MPS kernel hangs on Apple Silicon

**Cause**: PyTorch MPS backend incompatibility with the cross-encoder model.
**Fix**: Set `reranker_disabled = true` in `memory.toml` or `MEMORY_RERANKER_DISABLED=1`.

## Related

- [Search Pipeline](../concepts/search-pipeline.md) — How the search pipeline is structured
- [Add a Cron Job](add-a-cron-job.md) — Recurring maintenance tasks
- [Configuration](../reference/configuration.md) — Search and reranker settings
