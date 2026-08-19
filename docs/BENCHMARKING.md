# Unified Benchmarking Framework (2026 Edition)

Agentic-Memory features an end-to-end, reproducible benchmarking harness evaluating hybrid search, temporal reasoning, multi-hop inference, adversarial robustness, and 2026 SOTA agent trajectory recall.

All benchmarks evaluate the live, production 14-phase search orchestrator (`search_memories`), ensuring that code improvements and algorithmic enhancements directly translate to verified quality metrics.

---

## 1. Quick Start

Run the default suite in quick smoke-test mode (10 questions per suite):
```bash
venv/bin/python eval/run_benchmarks.py --quick
```

Run all suites in quick smoke-test mode:
```bash
venv/bin/python eval/run_benchmarks.py --suite all --quick
```

Run a specific benchmark suite with custom question limit:
```bash
venv/bin/python eval/longmemeval_v2_eval.py --quick
venv/bin/python eval/longmemeval_v2_eval.py --rebuild --tier small
venv/bin/python eval/locomo_eval.py --max-questions 50
venv/bin/python eval/run_benchmarks.py --suite longmemeval_v2 --limit 20
venv/bin/python eval/run_benchmarks.py --suite locomo --limit 50
venv/bin/python eval/run_benchmarks.py --suite beam --limit 30
venv/bin/python eval/run_benchmarks.py --suite adversarial
venv/bin/python eval/run_benchmarks.py --suite golden
```

Run full evaluation with cached database rebuild:
```bash
venv/bin/python eval/run_benchmarks.py --suite all --rebuild
```

Compare results against a baseline for regression gating:
```bash
venv/bin/python eval/run_benchmarks.py --suite all --compare eval/results/baseline_summary.json
```

---

## 2. Benchmark Suites

| Suite | Adapter | Description | Default Dataset Source | Key Metrics |
|---|---|---|---|---|
| **LoCoMo** | `LoCoMoAdapter` | 10 long conversations (~2000 QA pairs across 5 categories) testing multi-session evidence retrieval. | `eval/datasets/locomo10.json` (auto-downloaded) | `recall@k`, `precision@k`, `mrr`, `ndcg@k`, `lafs` |
| **LongMemEval-V2** | `LongMemEvalV2Adapter` | 2026 SOTA agent memory benchmark (451 questions, web & enterprise trajectories, 5 abilities). Requires trajectories downloaded via submodule. | `eval/longmemeval_v2/data/longmemeval-v2/` (`download_data.py`) | `overall_accuracy`, `token_f1`, `lafs`, `rubric_score` |
| **LongMemEval-S** | `LongMemEvalSAdapter` | Cleaned long-context conversational retrieval benchmark across 500 verified questions. | `eval/longmemeval_s/longmemeval_s_cleaned.json` | `recall@10`, `recall@50`, `ndcg@10`, `mrr` |
| **BEAM-10M** | `BEAMAdapter` | Multi-scale temporal fact tracking, entity attribution, and numeric synthesis over millions of tokens. | `eval/datasets/beam/` (auto-downloaded) | `overall_accuracy`, `per_type_accuracy` |
| **Adversarial** | `AdversarialAdapter` | Multi-hop inference, false premise abstention, and state collision stress testing across 4 distinct tracks. | Synthetic multi-track generator | `exact_match`, `abstention_accuracy` |
| **Golden** | `GoldenAdapter` | Curated production search quality regression net (325 memories, 276 test cases). | `eval/real_memory_golden_v2.json` | `recall@5`, `mrr`, `ndcg@5` |

---

## 3. Architecture & Speedups

### Fast Batch Indexing
Individual single-row insertions previously required 20+ minutes for large corpora. The unified engine uses `populate_eval_memory_indexes_batch()` from `eval/_fixtures.py`, batching:
- SentenceTransformer embeddings (`BAAI/bge-base-en-v1.5` / `bge-m3`) with GPU/MPS acceleration
- ColBERT token multi-vectors
- SPLADE sparse representations
- KG entities and facts
- SQLite FTS5 index records

Ingestion of 2,000+ sessions now completes in **<15 seconds**.

### Prebuilt Database Caching
Databases are cached by content and `SCHEMA_VERSION` MD5 hash under `eval/.cache/dbs/<suite>_<hash>.db`. Subsequent runs skip ingestion entirely, executing queries instantaneously against the pre-indexed database.
Use `--rebuild` or `--no-cache` to force recreation.

### Metric Standardization
- **Retrieval**: `recall@k`, `precision@k`, `mrr`, `ndcg@k` (k = 1, 5, 10, 20, 30, 50).
- **Generation & Rubrics**: `exact_match`, `substring_match`, `token_f1` (Counter multiset overlap), `rubric_score`, `overall_accuracy`.
- **Latency**: `mean`, `p50`, `p95`, `p99`, `max` latency measured in milliseconds.
- **LAFS (Latency-Adjusted F1 Score)**: $\text{LAFS} = \text{F1} \times \exp(-\text{latency\_ms} / \tau)$, penalizing slow retrievers.

---

## 4. Running Unit Tests

Run test verification of the benchmarking infrastructure:
```bash
venv/bin/python -m pytest eval/test_bench_engine.py -v
```

> **Note on Rule 1**: Benchmark ingestion harnesses populate test databases directly via `_fixtures.py` (`populate_eval_memory_indexes_batch()`) to evaluate isolated retrieval scenarios at scale. Production writes in runtime modules route strictly through `save_memory` / `save_memory_journal` as enforced by `eval/test_rule_enforcement.py`.
