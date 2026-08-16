#!/usr/bin/env python3
"""Unified Benchmark Runner for agentic-memory.

Executes standardized evaluation suites against the full 14-phase search orchestrator.
Supports LoCoMo, LongMemEval-V1/S, LongMemEval-V2, BEAM, Adversarial, and Golden retrieval.

Usage:
    venv/bin/python eval/run_benchmarks.py --quick
    venv/bin/python eval/run_benchmarks.py --suite locomo --limit 50
    venv/bin/python eval/run_benchmarks.py --suite all
    venv/bin/python eval/run_benchmarks.py --compare eval/results/baseline_summary.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Add repo root and eval dir to sys.path
EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

from _fixtures import set_benchmark_env
set_benchmark_env()

from bench.engine import BenchmarkHarness
from bench.adapters import ADAPTERS

SUITES = list(ADAPTERS.keys())
DEFAULT_SUITES = ["adversarial", "golden", "longmemeval_s"]
RESULTS_DIR = EVAL_DIR / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def print_comparison_table(summaries: list[dict]) -> None:
    """Print an aligned markdown comparison table across evaluated benchmark suites."""
    if not summaries:
        return

    print("\n" + "=" * 90)
    print("UNIFIED BENCHMARK EVALUATION SUMMARY TABLE")
    print("=" * 90)

    header = f"| {'Suite':<16} | {'Questions':<9} | {'Recall@10':<10} | {'MRR':<8} | {'Accuracy':<10} | {'p50 Lat (ms)':<12} | {'Wall Time':<10} |"
    sep = f"|{'-'*18}|{'-'*11}|{'-'*12}|{'-'*10}|{'-'*12}|{'-'*14}|{'-'*12}|"
    print(header)
    print(sep)

    for s in summaries:
        name = s.get("suite_name", "unknown")
        n_q = s.get("total_questions", 0)
        macro = s.get("macro_metrics", {})
        lat = s.get("latency_ms", {})
        wall = s.get("wall_time_seconds", 0.0)

        acc = macro.get("overall_accuracy", macro.get("exact_match", 0.0))
        rec10 = macro.get("recall@10", 0.0)
        mrr = macro.get("mrr", 0.0)
        p50 = lat.get("p50", 0.0)

        print(
            f"| {name:<16} | {n_q:<9} | {rec10:<10.4f} | {mrr:<8.4f} | {acc:<10.4f} | {p50:<12.2f} | {wall:<8.2f}s |"
        )
    print("=" * 90 + "\n")


def check_regression(
    current_summaries: list[dict],
    baseline_path: Path,
    threshold: float = 0.05,
) -> bool:
    """Compare current benchmark results with a baseline JSON file and assert no regression."""
    if not baseline_path.exists():
        print(f"WARNING: Baseline file {baseline_path} not found. Skipping regression check.")
        return True

    try:
        with open(baseline_path, "r", encoding="utf-8") as f:
            baseline_data = json.load(f)
    except Exception as exc:
        print(f"ERROR: Failed reading baseline {baseline_path}: {exc}")
        return False

    baseline_suites = {
        s.get("suite_name"): s
        for s in baseline_data.get("suites_evaluated", [])
        if "suite_name" in s
    }

    print("\n" + "=" * 90)
    print("REGRESSION COMPARISON WITH BASELINE")
    print(f"Baseline: {baseline_path}")
    print("=" * 90)

    header = f"| {'Suite':<16} | {'Metric':<14} | {'Baseline':<10} | {'Current':<10} | {'Delta':<10} | {'Status':<8} |"
    sep = f"|{'-'*18}|{'-'*16}|{'-'*12}|{'-'*12}|{'-'*12}|{'-'*10}|"
    print(header)
    print(sep)

    has_regression = False

    for curr in current_summaries:
        s_name = curr.get("suite_name")
        if s_name not in baseline_suites:
            continue
        base = baseline_suites[s_name]

        curr_macro = curr.get("macro_metrics", {})
        base_macro = base.get("macro_metrics", {})

        metrics_to_check = ["recall@10", "mrr", "overall_accuracy", "token_f1"]
        for m in metrics_to_check:
            if m in curr_macro and m in base_macro:
                c_val = curr_macro[m]
                b_val = base_macro[m]
                delta = c_val - b_val
                regressed = delta < -threshold
                if regressed:
                    has_regression = True
                status = "FAIL" if regressed else "PASS"
                print(
                    f"| {s_name:<16} | {m:<14} | {b_val:<10.4f} | {c_val:<10.4f} | {delta:<+10.4f} | {status:<8} |"
                )

    print("=" * 90 + "\n")
    if has_regression:
        print(f"REGRESSION DETECTED: One or more metrics degraded by > {threshold*100:.1f}%")
        return False
    else:
        print(f"REGRESSION CHECK PASSED: All metrics within acceptable threshold (<= {threshold*100:.1f}%)")
        return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unified agentic-memory benchmark execution engine"
    )
    parser.add_argument(
        "--suite",
        type=str,
        default=",".join(DEFAULT_SUITES),
        help=f"Comma-separated suites to run: {', '.join(SUITES)} or 'all' (default: {','.join(DEFAULT_SUITES)})",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Quick smoke test mode: evaluate max 10 questions per suite",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum questions to evaluate per suite",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not use cached prebuilt DBs (re-ingest from scratch)",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Force rebuild cached DBs",
    )
    parser.add_argument(
        "--compare",
        type=str,
        default=None,
        help="Path to baseline consolidated_summary.json for regression checking",
    )
    parser.add_argument(
        "--regression-threshold",
        type=float,
        default=0.05,
        help="Maximum allowable drop before failing regression check (default: 0.05)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Custom directory to store results",
    )
    args = parser.parse_args()

    # Determine limit
    limit = args.limit
    if args.quick and limit is None:
        limit = 10

    # Determine suites
    if args.suite.lower() == "all":
        selected_suites = SUITES
    else:
        selected_suites = [s.strip().lower() for s in args.suite.split(",") if s.strip()]

    out_dir = Path(args.output_dir) if args.output_dir else RESULTS_DIR
    harness = BenchmarkHarness(results_dir=out_dir)

    print(f"\nAgentic-Memory Unified Benchmark Harness Starting...")
    print(f"Suites to evaluate: {', '.join(selected_suites)}")
    print(f"Quick Mode:         {args.quick} (limit={limit})")
    print(f"Use Cache DB:       {not args.no_cache}")
    print(f"Force Rebuild:      {args.rebuild}")

    summaries = []
    t_global_start = time.time()

    try:
        for s_name in selected_suites:
            if s_name not in ADAPTERS:
                print(f"Skipping unknown suite: {s_name}")
                continue
            try:
                summary = harness.run_suite(
                    suite_name=s_name,
                    adapter_or_name=s_name,
                    max_questions=limit,
                    use_cache_db=not args.no_cache,
                    force_rebuild=args.rebuild,
                )
                summaries.append({
                    "suite_name": summary.suite_name,
                    "dataset_version": summary.dataset_version,
                    "total_questions": summary.total_questions,
                    "wall_time_seconds": summary.wall_time_seconds,
                    "latency_ms": summary.latency_ms,
                    "macro_metrics": summary.macro_metrics,
                    "category_metrics": summary.category_metrics,
                    "error": summary.error,
                })
            except Exception as exc:
                print(f"ERROR executing suite {s_name}: {exc}")
                import traceback
                traceback.print_exc()
    finally:
        harness.cleanup()

    total_time = time.time() - t_global_start
    print_comparison_table(summaries)

    consolidated_path = out_dir / "consolidated_summary.json"
    with open(consolidated_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "total_wall_time_seconds": round(total_time, 2),
                "suites_evaluated": summaries,
            },
            f,
            indent=2,
        )
    print(f"Consolidated summary saved to: {consolidated_path}\n")

    if args.compare:
        passed = check_regression(
            summaries,
            Path(args.compare),
            threshold=args.regression_threshold,
        )
        if not passed:
            sys.exit(1)


if __name__ == "__main__":
    main()
