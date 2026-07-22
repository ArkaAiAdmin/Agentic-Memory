# Benchmarks

Reproducible evaluation results for agentic-memory across three benchmark suites.

---

## LongMemEval_S (Pure Retrieval)

**Dataset**: [xiaowu0162/longmemeval-cleaned](https://github.com/xiaowu0162/longmemeval-cleaned) (MIT)  
**N**: 470 evaluable questions (500 total − 30 abstention)  
**Haystack**: ~48 sessions per question  
**Indexing unit**: whole session (turns joined by `\n`)  
**System**: BM25 (FTS5, unicode61) + cross-encoder `ms-marco-MiniLM-L-6-v2`, blend=0.6  
**Manifest**: `eval/longmemeval_s/MANIFEST.md5`  
**Eval harness**: `eval/longmemeval_s/run_eval.py`

### Headline (470q, BM25 + CE blend 0.6)

| Metric | Value |
|---|---|
| `recall_all@5` | 90.21% |
| **`recall_all@10`** | **95.32%** |
| `recall_all@30` | 98.09% |
| `recall_all@50` | 98.72% |
| `recall_any@10` | 99.15% |
| `ndcg_any@10` | 0.9223 |

### Per-Type Breakdown

| Type | n | recall_all@10 | ndcg_any@10 |
|---|---:|---:|---:|
| single-session-assistant | 56 | 100.00% | 1.0000 |
| single-session-user | 64 | 100.00% | 0.8499 |
| knowledge-update | 72 | 98.61% | 0.9665 |
| single-session-preference | 30 | 96.67% | 0.8056 |
| multi-session | 121 | 92.56% | 0.9488 |
| temporal-reasoning | 127 | 91.34% | 0.9018 |

### Prod Pipeline (50-Q subset, FTS + bge-base-en-v1.5 hybrid)

| Metric | Value |
|---|---|
| `recall_all@10` | 54.00% |
| `recall_any@10` | 54.00% |
| `ndcg_any@10` | 0.4434 |

The standalone BM25+CE harness (95.32%) significantly outperforms the prod pipeline (54%) because the cross-encoder reranker provides stronger ranking than the embedding-only hybrid path. The prod pipeline gap is a known area for improvement.

### Comparison vs Published Baselines

| System | Metric | Value | Notes |
|---|---|---:|---|
| **This work (BM25+CE)** | recall_all@10 | **95.32%** | 470q, session-level |
| Memory-Core BM25 | R@10 | 96.2% | 500q, BM25 only |
| Memory-Core Hybrid (BM25+dense RRF) | R@10 | 97.9% | 500q |
| agentmemory BM25+Vector | R@10 | 98.6% | recall_any, 500q |
| Prism (FTS5 + nomic-embed-text) | R@5 | 92.3% | recall_any@5, 470q |

**Note**: Published baselines use recall_any (at least one gold in top-k) on the full 500q set. Our recall_all@10 (all gold sessions must be in top-10) is a stricter metric. The 95.32% recall_all@10 is comparable to the published 96-98% recall_any@10.

---

## LoCoMo (Long Conversation Memory)

**Dataset**: [snap-research/locomo](https://github.com/snap-research/locomo) — 10 long conversations, 1986 QA pairs  
**Categories**: single-hop (282), multi-hop (321), temporal (96), open-domain (841), adversarial (446)  
**Eval script**: `eval/locomo_eval.py`  
**Results**: `eval/results/locomo-full-eval.json`

### Overall Recall@k

| k | Recall |
|---:|---:|
| 1 | 57.30% |
| 5 | 83.89% |
| **10** | **92.20%** |
| 20 | 97.03% |

### Per-Category Recall@10

| Category | n | Recall@10 |
|---|---:|---:|
| single-hop | 282 | 92.20% |
| multi-hop | 321 | 88.47% |
| **temporal** | **96** | **72.92%** |
| open-domain | 841 | 94.53% |
| adversarial | 446 | 94.62% |

The temporal-reasoning subset (72.92% at k=10) is the hardest category. Root-cause: these are inference queries ("Would X likely have Y?") that require finding sessions about a person's characteristics. The FTS layer's entity-anchored AND matching (`query_parser.py`) correctly finds gold sessions, but the pipeline's reranking phase reorders results and pushes them below top-10. Fixing the reranker to preserve entity-anchored matches would close this gap.

### Latency

| Percentile | Latency |
|---|---:|
| mean | 74.89ms |
| p50 | 73.91ms |
| p95 | 94.99ms |
| max | 132.29ms |

---

## BEAM (Board of Evaluation for Agent Memory)

**Scales**: 100K (10 sessions), 1M (100 sessions), 10M (1000 sessions)
**Questions**: 112 tracking questions per scale (100 current-value + 5 temporal + 1 multi-hop + 3 adversarial + 3 filler)
**Eval script**: `eval/beam/run_beam_eval.py`
**Results**: `eval/beam/results/beam-run.json`

### Results by Scale (Prod Pipeline — fact_lookup mode)

Evaluated through the prod `search_memories()` pipeline with `mode="fact_lookup"`: FTS5 AND-matching with OR fallback, recency ordering, temporal filtering, output envelope. Auto-detected for "What is the current X?" queries. Skips embedding fallback, CE reranking, and KG boost.

| Scale | Sessions | Questions | Accuracy | Avg Latency | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|
| 100K | 10 | 112 | **100.00%** | 0.4ms | 0.3ms | 0.6ms |
| 1M | 100 | 112 | **94.12%** | 1.3ms | 1.2ms | 2.7ms |
| 10M | 1000 | 112 | **87.50%** | 6.8ms | 6.3ms | 14.8ms |

**Latency methodology**: End-to-end per-question through `search_memories()` with `mode="fact_lookup"`. Includes connection pool acquisition, lightweight query parsing, FTS5 AND+OR matching, recency ordering, temporal filtering, and output envelope. First queries include ~8s model warmup. Steady-state latency reported above.

### Comparison Table

| System | 100K | 1M | 10M |
|---|---:|---:|---:|
| **This work (fact_lookup)** | **100.00%** | **94.12%** | **87.50%** |
| Cognee | 79.0% | — | — |
| Mem0 | — | 64.1% | — |

Cognee and Mem0 cells are from published baselines (Cognee 79% at 100K, Mem0 64.1% at 1M). Our result uses `fact_lookup` mode through the prod pipeline. Cognee/Mem0 numbers may use different retrieval strategies.

### Known Limitations

1. **Synthetic data**: BEAM sessions use a uniform "X is now Y" format with 100 distinct topics. FTS5 AND-matching on topic keywords is highly specific, making retrieval correct at any scale. A harder benchmark would use overlapping topics and paraphrased queries.
2. **Current-value only**: Questions ask "what is the current X?" — the correct answer is always the most recent session mentioning topic X. The benchmark does not test historical recall ("what was X before Y changed?").
3. **fact_lookup mode**: The eval uses `mode="fact_lookup"` which skips embedding, CE reranking, and KG boost. The full hybrid pipeline (`mode="hybrid"`) adds ~1.7s per query and may have different accuracy characteristics.

---

## Methodology Notes

- **LongMemEval_S**: Each question's haystack (~48 sessions) is indexed into a fresh in-memory FTS5 DB. No production DB is touched. BM25 top-50 candidates are rescored by cross-encoder. The eval harness (`run_eval.py`) is independent of the prod search pipeline.
- **LoCoMo**: Conversations are ingested as memory notes via the prod save path. Retrieval uses `search_memories` with FTS + embedding hybrid. Session-level recall: gold session must appear in top-k results.
---

## Benchmark Execution Commands & Infrastructure

All benchmark suites are fully automated and can be executed via the project virtual environment (`venv/`):

### 1. LongMemEval_S Benchmark (470 Questions Full Run & 66-Question Run)

```bash
# Full official 470-question LongMemEval_S benchmark using main production search pipeline:
PYTHONPATH=. ./venv/bin/python eval/longmemeval_s/run_eval_main_pipeline.py \
    --input eval/longmemeval_s/longmemeval_s_cleaned.json \
    --output eval/longmemeval_s/results/eval_main_pipeline_full.json

# Synthetic 66-question LongMemEval_S benchmark run:
PYTHONPATH=. ./venv/bin/python eval/run_longmemeval_s.py
```

### 2. LoCoMo Benchmark (Long Conversation Memory — 1,986 Questions)

```bash
# Optimized LoCoMo evaluation (10 long conversations, ~2,000 QA pairs):
MEMORY_WRITE_QUEUE_TIMEOUT=120.0 MEMORY_LLM_EXTRACTION=false \
PYTHONPATH=. ./venv/bin/python eval/locomo_eval.py --max-questions 50
```

### 3. BEAM Real Data Benchmark (BEAM-10M Dataset)

```bash
# Evaluate against real BEAM-10M HuggingFace dataset:
PYTHONPATH=. ./venv/bin/python eval/beam/run_beam_real.py --max-conversations 5
```

### 4. Golden Retrieval Benchmark (25 Query Test Cases)

```bash
# Run hybrid vector + FTS5 retrieval benchmark:
PYTHONPATH=. ./venv/bin/python eval/retrieval_benchmark.py
```

### 5. SOTA Multi-Agent Adversarial Suite (20 Edge Cases)

```bash
# Run 4-category adversarial resilience benchmark (Epistemic Abstention, Quantitative Synthesis, State Collision):
PYTHONPATH=. ./venv/bin/python eval/adversarial_eval.py
```

### 6. CRDT Projection & 10M Scalability Benchmark

```bash
# Systems projection unit & integration tests (88 tests):
PYTHONPATH=paper_pipeline ./venv/bin/python -m pytest paper_pipeline/test_pipeline.py paper_pipeline/test_adversarial.py -v

# Formal CK-CRDT proof counterexample suite (36 tests):
PYTHONPATH=paper_pipeline_2 ./venv/bin/python -m pytest paper_pipeline_2/test_adversarial.py -v

# 10 Million operation scale throughput & memory benchmark:
PYTHONPATH=paper_pipeline ./venv/bin/python paper_pipeline/benchmark.py
```
