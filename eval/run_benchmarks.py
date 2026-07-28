#!/usr/bin/env python3
"""Run all benchmarks sequentially with monitoring, polling, and consolidated reporting.

Usage:
    venv/bin/python eval/run_benchmarks.py
    venv/bin/python eval/run_benchmarks.py --quick   # use quick/smoke modes where available
"""

import json
import os
import subprocess
import sys
import time
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
RESULTS_DIR = HERE / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

VENV_PYTHON = Path(sys.executable)
if not VENV_PYTHON.exists():
    VENV_PYTHON = REPO_ROOT / "venv" / "bin" / "python"
    if not VENV_PYTHON.exists():
        VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

# ---------------------------------------------------------------------------
# Benchmark definitions: (name, script_path, args, timeout_s, description)
# ---------------------------------------------------------------------------
def _bench(name, script, args="", timeout=1800, desc=""):
    return (name, HERE / script, args, timeout, desc)

BENCHMARKS = [
    _bench("bench-search", "benchmarks/bench_search.py", timeout=600,
           desc="Search latency: p50/p95/p99 across corpus sizes"),
    _bench("bench-save", "benchmarks/bench_save.py", timeout=600,
           desc="Save latency: p50/p95/p99 across corpus sizes"),
    _bench("embedding", "embedding_benchmark.py", "--quick", timeout=600,
           desc="Embedding search speed, ANN recall, index build time"),
    _bench("profile-search", "profile_search.py", timeout=600,
           desc="Search profiling: per-phase latency breakdown"),
    _bench("retrieval", "retrieval_benchmark.py", timeout=900,
           desc="Retrieval metrics: precision@k, recall@k, MRR"),
    _bench("adversarial", "adversarial_memory_eval.py", timeout=900,
           desc="Adversarial eval: accuracy across attack categories"),
    _bench("golden-eval", "run_golden_eval.py", timeout=900,
           desc="Golden eval: search quality against curated dataset"),
    _bench("real-memory", "real_memory_eval.py", timeout=1200,
           desc="Real memory eval: end-to-end search quality on live DB"),
    _bench("longmemeval", "run_longmemeval_s.py", timeout=1800,
           desc="LongMemEval_S: hybrid vs FTS5 baseline comparison"),
    _bench("locomo", "locomo_eval.py", "--max-questions 50", timeout=3600,
           desc="LoCoMo: evidence-based retrieval recall@k"),
    _bench("perf-envelope", "perf_envelope.py", "--quick", timeout=1800,
           desc="Performance envelope: latency across corpus sizes"),
    _bench("perf-envelope-v3", "perf_envelope_v3.py", "--quick", timeout=1800,
           desc="Performance envelope v3: refined latency profiling"),
    _bench("beam-eval", "beam/run_beam_eval.py", "--all-scales", timeout=7200,
           desc="BEAM eval: synthetic long-context memory at 100K/1M/10M"),
    _bench("beam-real", "beam/run_beam_real.py", "", timeout=7200,
           desc="BEAM real: BEAM-10M dataset evaluation"),
    _bench("longmemeval-v2-A", "longmemeval_s/run_eval_v2.py",
           "--input eval/longmemeval_s/longmemeval_s_cleaned.json --output eval/longmemeval_s/results/eval_A.json --variant A --limit 50",
           timeout=1800, desc="LongMemEval V2 variant A: implicit date"),
    _bench("longmemeval-v2-B", "longmemeval_s/run_eval_v2.py",
           "--input eval/longmemeval_s/longmemeval_s_cleaned.json --output eval/longmemeval_s/results/eval_B.json --variant B --limit 50",
           timeout=1800, desc="LongMemEval V2 variant B: explicit date"),
    _bench("longmemeval-v2-AB", "longmemeval_s/run_eval_v2.py",
           "--input eval/longmemeval_s/longmemeval_s_cleaned.json --output eval/longmemeval_s/results/eval_AB.json --variant AB --limit 50",
           timeout=1800, desc="LongMemEval V2 variant AB: both"),
    _bench("longmemeval-v4", "longmemeval_s/run_full_eval_v4.py",
           f"eval/longmemeval_s/longmemeval_s_cleaned.json 50",
           timeout=3600, desc="LongMemEval V4: full pipeline with bge-base"),
]

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
BASE_ENV = os.environ.copy()
BASE_ENV["KMP_DUPLICATE_LIB_OK"] = "TRUE"
BASE_ENV["OMP_NUM_THREADS"] = "1"
BASE_ENV["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"
BASE_ENV["MEMORY_FAIL_ON_INTEGRITY_DRIFT"] = "0"
BASE_ENV["PYTHONUNBUFFERED"] = "1"

# ---------------------------------------------------------------------------
# Poll + watchdog
# ---------------------------------------------------------------------------
POLL_INTERVAL = 30.0  # seconds between progress checks

def _run_with_monitoring(name: str, script_path: Path, args: str, timeout_s: int) -> dict:
    cmd = [str(VENV_PYTHON), str(script_path)]
    if args:
        cmd.extend(args.split())
    log_path = RESULTS_DIR / f"{name}.log"
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=BASE_ENV,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    with open(log_path, "w") as log:
        start = time.monotonic()
        last_output = start
        killed = False
        out_lines = []
        while True:
            try:
                line = proc.stdout.readline()
                if line:
                    out_lines.append(line)
                    log.write(line)
                    log.flush()
                    last_output = time.monotonic()
                    print(f"  [{name}] {line.rstrip()}", flush=True)
                else:
                    proc.poll()
                    if proc.returncode is not None:
                        break
            except Exception:
                break
            elapsed = time.monotonic() - start
            since_last = time.monotonic() - last_output
            if since_last >= timeout_s:
                print(f"\n  ⚠ [{name}] NO OUTPUT FOR {timeout_s}s — KILLING (elapsed: {elapsed:.0f}s)", flush=True)
                proc.kill()
                killed = True
                break
            if elapsed >= 3600:
                print(f"\n  ⚠ [{name}] EXCEEDED 1-HOUR HARD CAP — KILLING", flush=True)
                proc.kill()
                killed = True
                break
        proc.stdout.close()
        proc.wait(timeout=10)
        dur = time.monotonic() - start
        full_output = "".join(out_lines)
        returncode = -1 if killed else proc.returncode
        return {
            "name": name,
            "returncode": returncode,
            "killed": killed,
            "duration_s": round(dur, 1),
            "output": full_output,
            "log_path": str(log_path),
        }

# ---------------------------------------------------------------------------
# Results collector
# ---------------------------------------------------------------------------
def _collect_metrics() -> dict:
    """Read all result JSON files and consolidate metrics."""
    metrics = {}
    if (RESULTS_DIR / "bench-save.json").exists():
        with open(RESULTS_DIR / "bench-save.json") as f:
            metrics["save"] = json.load(f)
    if (RESULTS_DIR / "bench-search.json").exists():
        with open(RESULTS_DIR / "bench-search.json") as f:
            metrics["search"] = json.load(f)
    if (RESULTS_DIR / "retrieval_benchmark_results.json").exists():
        with open(RESULTS_DIR / "retrieval_benchmark_results.json") as f:
            metrics["retrieval"] = json.load(f)
    if (RESULTS_DIR / "bench-embeddings.json").exists():
        with open(RESULTS_DIR / "bench-embeddings.json") as f:
            metrics["embedding"] = json.load(f)
    if (RESULTS_DIR / "adversarial_eval_results.json").exists():
        with open(RESULTS_DIR / "adversarial_eval_results.json") as f:
            metrics["adversarial"] = json.load(f)
    if (RESULTS_DIR / "longmemeval-s-run.json").exists():
        with open(RESULTS_DIR / "longmemeval-s-run.json") as f:
            metrics["longmemeval"] = json.load(f)
    if (RESULTS_DIR / "locomo-eval-50.json").exists():
        with open(RESULTS_DIR / "locomo-eval-50.json") as f:
            metrics["locomo"] = json.load(f)
    if (RESULTS_DIR / "retrieval_results.json").exists():
        with open(RESULTS_DIR / "retrieval_results.json") as f:
            metrics["retrieval_results"] = json.load(f)
    return metrics

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    import argparse
    ap = argparse.ArgumentParser(description="Run all benchmarks sequentially")
    ap.add_argument("--quick", action="store_true", help="Use quick/smoke modes where available")
    ap.add_argument("--benchmark", type=str, default="",
                    help="Run a single benchmark (by name). Omit to run all.")
    ap.add_argument("--list", action="store_true", help="List available benchmarks and exit")
    args = ap.parse_args()

    if args.list:
        print("Available benchmarks:")
        for name, script, b_args, timeout, desc in BENCHMARKS:
            print(f"  {name:20s}  {desc}")
            print(f"                      {' ' * 4}{script} {b_args}")
        return 0

    selected = BENCHMARKS
    if args.benchmark:
        selected = [b for b in BENCHMARKS if b[0] == args.benchmark]
        if not selected:
            print(f"Unknown benchmark: {args.benchmark}")
            print(f"Available: {', '.join(b[0] for b in BENCHMARKS)}")
            return 1

    results = []
    total_start = time.monotonic()

    print(f"{'=' * 70}")
    print(f"  AGENTIC-MEMORY BENCHMARKS")
    print(f"  Sequential runner — {len(selected)} benchmark(s)")
    print(f"  Python: {VENV_PYTHON}")
    print(f"  Quick mode: {'YES' if args.quick else 'no'}")
    print(f"{'=' * 70}\n")

    for name, script, b_args, timeout, desc in selected:
        if args.quick and b_args:
            actual_args = b_args
        elif args.quick and not b_args:
            actual_args = "--quick"
        else:
            actual_args = b_args

        print(f"\n{'─' * 70}")
        print(f"  ▶ {name}")
        print(f"    {desc}")
        print(f"    {script} {actual_args}")
        print(f"    Timeout: {timeout}s  |  Poll interval: {POLL_INTERVAL}s")
        print(f"{'─' * 70}\n")

        result = _run_with_monitoring(name, script, actual_args, timeout)
        results.append(result)

        status = "✓" if result["returncode"] == 0 else "✗"
        killed = " [KILLED by watchdog]" if result["killed"] else ""
        print(f"\n  [{status}] {name} — exit={result['returncode']}, duration={result['duration_s']}s{killed}")

    total_dur = time.monotonic() - total_start

    # ── Summary ──────────────────────────────────────────────────────────
    print(f"\n\n{'=' * 70}")
    print(f"  SUMMARY")
    print(f"{'=' * 70}")
    passed = [r for r in results if r["returncode"] == 0 and not r["killed"]]
    failed = [r for r in results if r["returncode"] != 0 or r["killed"]]
    print(f"  Passed: {len(passed)}  |  Failed: {len(failed)}  |  Total time: {total_dur:.0f}s ({total_dur/60:.1f}m)")
    for r in results:
        status = "✓ PASS" if r["returncode"] == 0 and not r["killed"] else "✗ FAIL"
        print(f"  {status:8s}  {r['name']:20s}  {r['duration_s']:>8.1f}s")
        if r["returncode"] != 0 and not r["killed"]:
            last_lines = r["output"].strip().split("\n")[-5:]
            for ll in last_lines:
                print(f"           {ll}")

    # ── Metrics report ───────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print(f"  SEARCH & RETRIEVAL METRICS REPORT")
    print(f"{'=' * 70}")
    metrics = _collect_metrics()

    if "search" in metrics:
        s = metrics["search"].get("results", {})
        print(f"\n  ── Search Latency (bench-search) ──")
        for sz, data in sorted(s.items()):
            lat = data.get("latency_ms", {})
            def _f(v, default="?"):
                if isinstance(v, (int, float)):
                    return f"{v:>8.2f}ms"
                return str(v)
            print(f"    corpus={sz:>6}  p50={_f(lat.get('p50'))}  p95={_f(lat.get('p95'))}  "
                  f"p99={_f(lat.get('p99'))}  max={_f(lat.get('max'))}")

    if "save" in metrics:
        s = metrics["save"].get("results", {})
        print(f"\n  ── Save Latency (bench-save) ──")
        for sz, data in sorted(s.items()):
            lat = data.get("latency_ms", {})
            def _f(v, default="?"):
                if isinstance(v, (int, float)):
                    return f"{v:>8.2f}ms"
                return str(v)
            print(f"    corpus={sz:>6}  p50={_f(lat.get('p50'))}  p95={_f(lat.get('p95'))}  "
                  f"p99={_f(lat.get('p99'))}  max={_f(lat.get('max'))}")

    if "retrieval" in metrics:
        r = metrics["retrieval"]
        print(f"\n  ── Retrieval Metrics (retrieval_benchmark) ──")
        phases = r.get("phases", {})
        for phase, data in sorted(phases.items()):
            p = data.get("precision_at_5", "?")
            r2 = data.get("recall_at_5", "?")
            mrr = data.get("mrr", "?")
            lat = data.get("latency_ms", {})
            print(f"    {phase:20s}  P@5={p:<8}  R@5={r2:<8}  MRR={mrr:<8}  "
                  f"latency={lat}")

    if "embedding" in metrics:
        e = metrics["embedding"]
        print(f"\n  ── Embedding Performance (embedding_benchmark) ──")
        for k in ("embedding_speed", "memory_index_build_s", "chunk_index_build_s"):
            if k in e:
                print(f"    {k}: {e[k]}")
        search_recall = e.get("search_recall", {})
        if search_recall:
            print(f"    search_recall (ANN vs brute-force):")
            for sz, v in sorted(search_recall.items()):
                print(f"      corpus={sz:>6}  recall={v}")

    if "adversarial" in metrics:
        a = metrics["adversarial"]
        print(f"\n  ── Adversarial Eval ──")
        print(f"    overall_accuracy: {a.get('overall_accuracy', '?')}")
        print(f"    avg_latency_ms:   {a.get('avg_latency_ms', '?')}")
        cat_acc = a.get("category_accuracy", {})
        for cat, acc in sorted(cat_acc.items()):
            print(f"    {cat:30s}  accuracy={acc}")

    if "longmemeval" in metrics:
        lm = metrics["longmemeval"]
        print(f"\n  ── LongMemEval_S ──")
        print(f"    hybrid_score:          {lm.get('hybrid_score', '?')}")
        print(f"    baseline_fts5_score:   {lm.get('baseline_ft5_score', lm.get('baseline_fts5_score', '?'))}")
        print(f"    hybrid_recall_at_k:    {lm.get('hybrid_recall_at_k', '?')}")
        print(f"    baseline_recall_at_k:  {lm.get('baseline_recall_at_k', '?')}")
        print(f"    wall_time_seconds:     {lm.get('wall_time_seconds', '?')}")
        p50p95 = lm.get("p50_p95_ms", [])
        if len(p50p95) >= 2:
            print(f"    latency_p50:           {p50p95[0]}ms")
            print(f"    latency_p95:           {p50p95[1]}ms")

    if "locomo" in metrics:
        lc = metrics["locomo"]
        print(f"\n  ── LoCoMo Eval (50 questions) ──")
        for k in sorted(lc.keys()):
            if k != "detailed":
                print(f"    {k}: {lc[k]}")
        detailed = lc.get("detailed", {})
        if detailed and isinstance(detailed, dict):
            for k in sorted(detailed.keys()):
                if isinstance(detailed[k], (int, float)):
                    print(f"    detailed.{k}: {detailed[k]}")

    # Print log paths for failed benchmarks
    for r in failed:
        print(f"\n  📄 Full log for failed benchmark '{r['name']}': {r['log_path']}")

    print(f"\n{'=' * 70}\n")

    return 0 if len(failed) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
