# Evaluation & Benchmarking Framework (`eval/`)

All evaluation benchmarks, datasets, results, and LLM-as-a-judge quality frameworks are consolidated under this single directory.

## Directory Structure

```
eval/
├── datasets/                 # Benchmark datasets & synthetic test sets
│   ├── locomo10.json         # LoCoMo 10 long-conversation benchmark
│   └── longmemeval_s_synth.jsonl # LongMemEval_S synthetic dataset
├── results/                  # Benchmark execution results (JSON/reports)
├── gold/                     # Gold-standard baseline datasets
├── beam/                     # BEAM (Board of Evaluation for Agent Memory) benchmark
│   ├── run_beam_eval.py      # Synthetic BEAM benchmark runner
│   └── run_beam_real.py      # BEAM-10M real-data benchmark runner
├── benchmarks/               # Low-level performance micro-benchmarks
│   ├── bench_save.py         # Write/save throughput benchmark
│   └── bench_search.py       # Search latency benchmark
├── longmemeval_s/            # LongMemEval_S evaluation suite
│   ├── run_eval_main_pipeline.py # Evaluator against the 14-phase search orchestrator
│   └── ...                   # Strategy & baseline evaluation runners
├── locomo_eval.py            # LoCoMo benchmark runner (Recall@k across conversation sessions)
├── real_memory_eval.py      # Real-memory SOTA golden evaluation harness
├── adversarial_memory_eval.py # Adversarial & Multi-Hop state collision benchmark
├── retrieval_benchmark.py   # Pipeline retrieval precision/recall/MRR benchmark
├── embedding_benchmark.py   # Embedding search & vector index benchmark
├── perf_envelope.py          # Latency & throughput envelope profiler
├── eval_judge.py             # LLM-as-a-judge quality evaluation framework
├── _fixtures.py              # Test database bootstrapping & benchmark environment setup
├── conftest.py               # Pytest configuration & test session setup
└── run_full_suite.py         # Subprocess-isolated runner for the test suite
```

## Running Benchmarks

### SOTA & Real Memory Evaluation
```bash
python3 eval/real_memory_eval.py
```

### LoCoMo Benchmark
```bash
python3 eval/locomo_eval.py --max-questions 50
```

### Adversarial Benchmark
```bash
python3 eval/adversarial_memory_eval.py
```

### LongMemEval_S Benchmark
```bash
python3 eval/run_longmemeval_s.py
```

### Full Test Suite Execution
```bash
python3 eval/run_full_suite.py
```
