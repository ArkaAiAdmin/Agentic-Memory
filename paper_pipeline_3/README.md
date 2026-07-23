# Conflict-Free Multi-Agent Memory: Reproducibility Guide

## System Requirements

- **Python:** 3.10 or later (standard library only — no external dependencies)
- **Pandoc:** 3.0+ with `xelatex` PDF engine (for PDF generation)
- **TeX Live / MacTeX:** Required by `xelatex` for PDF rendering
- **OS:** macOS, Linux, or Windows (WSL)

## Quick Start

```bash
# Run the full benchmark suite (reproduces all tables in Section 9)
python3 benchmark.py

# Generate the paper PDF
make pdf

# Generate the double-blind artifact zip
make artifact-double-blind
```

## Reproducing Paper Results

The benchmark reproduces all evaluation results from Section 9 of the paper. Each function maps to a specific subsection:

| Paper Section | Benchmark Function | Description |
|---|---|---|
| §9.1 Baseline Comparison | `compare_baselines()` | Naive vs LWW vs full pipeline on 100K ops |
| §9.2 Convergence | `evaluate_convergence()` | Delivery-order permutation testing (1,200 permutations) |
| §9.3 Latency and Memory | `measure_latency_percentiles()`, `measure_memory_usage()` | p50/p95/p99 latency, memory at scale |
| §9.3 Production Scaling | `measure_latency_percentiles()` (SQLite mode) | 100K → 10M ops with SQLite |
| §9.4 Writer Scaling | `measure_concurrent_writer_scaling()` | Throughput vs agent count (2–32) |
| §9.5 System Comparison | (analytical — see Table 2 in paper) | Comparison against Zep/Mem0/Letta/Hindsight/Honcho/Yjs/Automerge |
| §9.6 Retrieval Accuracy | (production evaluation — see benchmark_report.md) | BEAM, LongMemEval_S, LoCoMo, Golden |
| §9.7 Adversarial Robustness | (production evaluation — see benchmark_report.md) | 20 adversarial scenarios |
| §9.8 Ablation Study | `run_ablation()` | Phase-by-phase contribution measurement |

## Expected Output

Running `python3 benchmark.py` produces:

1. **Baseline Comparison** — 3 rows: naive (16,323 entities, 15,323 dupes), centralized LWW (same), full pipeline (1,000 entities, 0 dupes)
2. **Convergence** — 1,200 permutations, 0 divergences, 0 orphan edges
3. **Latency Percentiles** — p50 ≈ 300ms, p95 ≈ 330ms, throughput ≈ 330K ops/s
4. **Memory Usage** — 100K: ~2.7 MB, 1M: ~20.3 MB
5. **Writer Scaling** — 2–32 agents, <10% throughput degradation
6. **Ablation Study** — Phase 1: 16,323 entities/15,323 dupes; Phase 1+2: 1,000 entities/18,834 orphans; Full: 1,000 entities/0 orphans

Expected runtime: ~60 seconds on Apple Silicon M-series.

## Test Suite

The companion test suites verify the pipeline correctness properties:

```bash
# Paper 1: Pipeline correctness (51 tests)
python3 -m pytest ../paper_pipeline/test_pipeline.py -v

# Paper 1: Adversarial robustness (37 tests)
python3 -m pytest ../paper_pipeline/test_adversarial.py -v

# Paper 2: CK-CRDT framework + counterexamples (36 tests)
python3 -m pytest ../paper_pipeline_2/test_adversarial.py -v
```

## Artifact Contents

| File | Description |
|---|---|
| `Content-Keyed-CRDTs.md` | Paper source (Markdown) |
| `Content-Keyed-CRDTs.pdf` | Paper PDF |
| `benchmark.py` | Reproducible benchmark (697 lines, stdlib only) |
| `Makefile` | PDF and artifact generation |
| `README.md` | This file |

## Production System

The paper describes a production deployment in `agentic-memory`, a local-first multi-agent memory service. The production code is in:

- `kg/kg_crdt.py` — CRDT merge engine
- `save_pipeline/` — Write pipeline orchestrator
- `infra/` — SQLite persistence, write queue, search indices

The benchmark in this artifact is a self-contained reproduction of the paper's evaluation using only the Python standard library. It does not require the production system.

## License

- Paper text: CC-BY-4.0
- Benchmark code: Apache-2.0
