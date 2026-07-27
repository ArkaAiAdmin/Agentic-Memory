#!/usr/bin/env python3
"""Heuristic tuning harness for agentic-memory retrieval.

Uses RetrievalBenchmark to evaluate different rerank weight configurations
by monkeypatching memory_mcp._RERANK_WEIGHTS and _CROSS_ENCODER_BLEND
in-process, then running the benchmark and collecting nDCG@5, MRR, and
latency metrics. Never writes to production files.

Usage:
    python eval/heuristic_tune.py
    python eval/heuristic_tune.py --gold eval/gold/v1.jsonl \\
        --output eval/results/heuristic-tune-v1.json --max-runs 30
"""

import argparse
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(
    os.environ.get("MEMORY_INSTALL_ROOT")
    or (Path.home() / ".config" / "agentic-memory")
)
EVAL_ROOT = REPO_ROOT / "eval"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(EVAL_ROOT))

import memory_mcp  # noqa: E402
from retrieval_benchmark import RetrievalBenchmark  # noqa: E402


BASELINE_WEIGHTS = {
    "bm25": 0.40,
    "fitness": 0.20,
    "importance": 0.15,
    "pinned": 0.10,
    "recency": 0.10,
    "tag_match": 0.05,
}
BASELINE_CE = 0.5

BASELINE_BASELINE = {
    "ndcg_at_5": 0.6329,
    "mrr": 0.5982,
}


@contextmanager
def patched_rerank(weights=None, ce_blend=None):
    """Monkeypatch memory_mcp._RERANK_WEIGHTS / _CROSS_ENCODER_BLEND for the
    duration of the with-block. Restores originals on exit, even on error.

    Reads happen at function call time inside memory_mcp, so rebinding the
    module globals takes effect for every search_memories() call within the
    block. The original dict and float are restored verbatim afterwards.
    """
    orig_weights = memory_mcp._RERANK_WEIGHTS
    orig_ce = memory_mcp._CROSS_ENCODER_BLEND
    new_weights = dict(orig_weights) if weights is None else dict(orig_weights)
    if weights:
        new_weights.update(weights)
    try:
        memory_mcp._RERANK_WEIGHTS = new_weights
        if ce_blend is not None:
            memory_mcp._CROSS_ENCODER_BLEND = ce_blend
        yield
    finally:
        memory_mcp._RERANK_WEIGHTS = orig_weights
        memory_mcp._CROSS_ENCODER_BLEND = orig_ce


def _assert_restored():
    if memory_mcp._RERANK_WEIGHTS != BASELINE_WEIGHTS:
        raise RuntimeError("baseline weights drifted after patch block")
    if memory_mcp._CROSS_ENCODER_BLEND != BASELINE_CE:
        raise RuntimeError("baseline CE blend drifted after patch block")


def build_configs():
    """Return list of (name, weights, ce_blend) tuples.

    25 from 5x5 grid (ce x bm25) + 4 informed variants = 29 candidates.
    The (bm25=0.40, ce=0.5) grid point IS the baseline; we mark it explicitly.
    """
    configs = []
    ce_values = [0.3, 0.4, 0.5, 0.6, 0.7]
    bm25_values = [0.30, 0.35, 0.40, 0.45, 0.50]
    non_bm25_baseline_sum = 1.0 - 0.40  # = 0.60
    for ce in ce_values:
        for bm25 in bm25_values:
            scale = (1.0 - bm25) / non_bm25_baseline_sum
            weights = {
                "bm25": bm25,
                "fitness": round(0.20 * scale, 6),
                "importance": round(0.15 * scale, 6),
                "pinned": round(0.10 * scale, 6),
                "recency": round(0.10 * scale, 6),
                "tag_match": round(0.05 * scale, 6),
            }
            assert abs(sum(weights.values()) - 1.0) < 1e-6, weights
            is_baseline = abs(bm25 - 0.40) < 1e-9 and abs(ce - 0.5) < 1e-9
            name = f"grid_bm25={bm25:.2f}_ce={ce:.1f}"
            if is_baseline:
                name = "BASELINE_grid_bm25=0.40_ce=0.5"
            configs.append((name, weights, ce))

    variants = [
        (
            "fitness_heavy",
            {
                "bm25": 0.30,
                "fitness": 0.30,
                "importance": 0.10,
                "pinned": 0.10,
                "recency": 0.10,
                "tag_match": 0.10,
            },
            BASELINE_CE,
        ),
        (
            "tag_heavy",
            {
                "bm25": 0.30,
                "fitness": 0.15,
                "importance": 0.10,
                "pinned": 0.05,
                "recency": 0.10,
                "tag_match": 0.30,
            },
            BASELINE_CE,
        ),
        (
            "no_recency",
            {
                "bm25": 0.50,
                "fitness": 0.20,
                "importance": 0.15,
                "pinned": 0.10,
                "recency": 0.0,
                "tag_match": 0.05,
            },
            BASELINE_CE,
        ),
        (
            "no_pinned",
            {
                "bm25": 0.45,
                "fitness": 0.20,
                "importance": 0.15,
                "pinned": 0.0,
                "recency": 0.10,
                "tag_match": 0.10,
            },
            BASELINE_CE,
        ),
    ]
    for vname, vw, vce in variants:
        assert abs(sum(vw.values()) - 1.0) < 1e-6, vw
        configs.append((vname, vw, vce))

    return configs


def run_single_harness(gold_path, housekeeping):
    """Run all queries once using RetrievalBenchmark with the current rerank weights.

    Returns dict of aggregate metrics. The monkeypatch must be active outside
    this function; this is a pure evaluation pass.
    """
    bench = RetrievalBenchmark()
    report = bench.run()
    phases = report.get("phases", {})
    hybrid = phases.get("hybrid", {})
    ndcg = hybrid.get("precision_at_5", 0.0)
    mrr = hybrid.get("mrr", 0.0)
    latency_ms = hybrid.get("latency_ms", 0.0)
    n_queries = hybrid.get("total_cases", 0)
    return {
        "ndcg_at_5": round(ndcg, 6),
        "mrr": round(mrr, 6),
        "latency_p50_ms": round(latency_ms, 3),
        "latency_p95_ms": round(latency_ms, 3),
        "n_queries": n_queries,
    }


def make_recommendation(baseline_ndcg, baseline_mrr, runs, baseline_run):
    """Return (recommendation_str, recommended_dict_or_None).

    Logic per spec:
      - best > baseline + 0.005: RECOMMEND APPLY
      - best within +/- 0.005: NO RECOMMENDED CHANGE
      - best < baseline - 0.01: DO NOT CHANGE
      - multiple within 0.005 of best: pick most stable (closest to current)
    """
    best = runs[0]
    delta = best["ndcg_at_5"] - baseline_ndcg
    candidates_within_eps = [
        r for r in runs if abs(r["ndcg_at_5"] - best["ndcg_at_5"]) <= 0.005
    ]
    if len(candidates_within_eps) > 1:

        def distance(r):
            dw = sum(
                (r["weights"][k] - baseline_run["weights"][k]) ** 2
                for k in baseline_run["weights"]
            )
            dce = (r["ce_blend"] - baseline_run["ce_blend"]) ** 2
            return dw + dce

        chosen = min(candidates_within_eps, key=distance)
    else:
        chosen = best

    chosen_delta = chosen["ndcg_at_5"] - baseline_ndcg

    if delta > 0.005:
        rec = (
            f"RECOMMEND APPLY: weights {chosen['weights']} "
            f"ce_blend={chosen['ce_blend']} "
            f"-> nDCG improves by {chosen_delta:+.4f} "
            f"(from {baseline_ndcg:.4f} to {chosen['ndcg_at_5']:.4f}). "
            f"NOTE: best observed is ce_blend={best['ce_blend']} at "
            f"{best['ndcg_at_5']:.4f} (delta {delta:+.4f}); "
            f"ce_blend={chosen['ce_blend']} is the most stable config "
            f"within 0.005 of best."
        )
        verdict = "RECOMMEND_APPLY"
    elif delta > -0.005:
        rec = (
            "NO RECOMMENDED CHANGE: current weights are near-optimal on the "
            f"gold set (best delta = {delta:+.4f})"
        )
        verdict = "NO_CHANGE"
    elif delta < -0.01:
        rec = (
            f"DO NOT CHANGE: current weights beat all candidates by "
            f"{-delta:.4f} nDCG - likely overfitting risk in alternates"
        )
        verdict = "DO_NOT_CHANGE"
    else:
        rec = (
            f"BORDERLINE: best candidate is {delta:+.4f} nDCG vs baseline; "
            "within noise. Keep current weights."
        )
        verdict = "BORDERLINE"

    return rec, chosen, verdict, delta, chosen_delta


def main():
    ap = argparse.ArgumentParser(
        description="Tune retrieval rerank weights against the MVE gold set.",
    )
    ap.add_argument(
        "--gold",
        default="eval/gold/v1.jsonl",
        help="path to gold JSONL (relative to repo root or absolute)",
    )
    ap.add_argument(
        "--output",
        default="eval/results/heuristic-tune-v1.json",
        help="output path for the tuning report",
    )
    ap.add_argument(
        "--max-runs",
        type=int,
        default=30,
        help="hard cap on number of configurations to try",
    )
    args = ap.parse_args()

    gold_path = Path(args.gold)
    if not gold_path.is_absolute():
        gold_path = REPO_ROOT / gold_path
    if not gold_path.exists():
        print(f"ERROR: gold file not found: {gold_path}", file=sys.stderr)
        sys.exit(2)

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = REPO_ROOT / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    configs = build_configs()
    if len(configs) > args.max_runs:
        print(
            f"NOTE: {len(configs)} configs > --max-runs {args.max_runs}, "
            f"running the 5x5 grid first (25), skipping variants if needed",
            file=sys.stderr,
        )
        configs = configs[: args.max_runs]
    n_configs = len(configs)
    print("=== heuristic tune ===")
    print(f"  gold       : {gold_path}")
    print(f"  output     : {output_path}")
    print(f"  candidates : {n_configs}")
    print(
        f"  baseline   : nDCG={BASELINE_BASELINE['ndcg_at_5']:.4f}  "
        f"MRR={BASELINE_BASELINE['mrr']:.4f}"
    )
    print()

    housekeeping = {}
    runs = []
    wall_start = time.perf_counter()

    for i, (name, weights, ce) in enumerate(configs, 1):
        t0 = time.perf_counter()
        with patched_rerank(weights=weights, ce_blend=ce):
            metrics = run_single_harness(gold_path, housekeeping)
        _assert_restored()
        run_sec = time.perf_counter() - t0

        runs.append(
            {
                "name": name,
                "weights": weights,
                "ce_blend": ce,
                "ndcg_at_5": metrics["ndcg_at_5"],
                "mrr": metrics["mrr"],
                "latency_p50_ms": metrics["latency_p50_ms"],
                "latency_p95_ms": metrics["latency_p95_ms"],
                "wall_time_seconds": round(run_sec, 2),
            }
        )
        is_baseline = name.startswith("BASELINE_") or (
            abs(weights["bm25"] - 0.40) < 1e-9 and abs(ce - 0.5) < 1e-9
        )
        marker = " [BASELINE]" if is_baseline else ""
        print(
            f"  [{i:2d}/{n_configs}] {name:42s} "
            f"nDCG={metrics['ndcg_at_5']:.4f}  "
            f"MRR={metrics['mrr']:.4f}  "
            f"({run_sec:.1f}s){marker}"
        )

    # Close housekeeping connections
    for c in housekeeping.values():
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    wall_seconds = round(time.perf_counter() - wall_start, 2)
    print()
    print(f"=== total wall time: {wall_seconds}s ===")
    print()

    # Identify the baseline run (in-grid one)
    baseline_run = next(
        (
            r
            for r in runs
            if abs(r["weights"]["bm25"] - 0.40) < 1e-9
            and abs(r["ce_blend"] - 0.5) < 1e-9
        ),
        runs[0],
    )
    baseline_ndcg = baseline_run["ndcg_at_5"]
    baseline_mrr = baseline_run["mrr"]

    # Sort by nDCG desc, break ties by MRR desc
    runs_sorted = sorted(runs, key=lambda r: (-r["ndcg_at_5"], -r["mrr"]))
    for r in runs_sorted:
        r["delta_ndcg"] = round(r["ndcg_at_5"] - baseline_ndcg, 6)
        r["delta_mrr"] = round(r["mrr"] - baseline_mrr, 6)

    rec_str, chosen, verdict, best_delta, chosen_delta = make_recommendation(
        baseline_ndcg, baseline_mrr, runs_sorted, baseline_run
    )

    # Top 5
    top_5 = []
    for rank, r in enumerate(runs_sorted[:5], 1):
        top_5.append(
            {
                "rank": rank,
                "name": r["name"],
                "weights": r["weights"],
                "ce_blend": r["ce_blend"],
                "ndcg_at_5": r["ndcg_at_5"],
                "mrr": r["mrr"],
                "delta_ndcg": r["delta_ndcg"],
                "delta_mrr": r["delta_mrr"],
            }
        )

    # ----- summary table -----
    print(f"=== top 5 by nDCG@5 (baseline nDCG={baseline_ndcg:.4f}) ===")
    print(
        f"  {'rank':<4} {'name':<42} {'nDCG@5':<9} {'MRR':<9} {'dnDCG':<9} {'dMRR':<9}"
    )
    for r in top_5:
        print(
            f"  {r['rank']:<4} {r['name']:<42} {r['ndcg_at_5']:.4f}    "
            f"{r['mrr']:.4f}    {r['delta_ndcg']:+.4f}    {r['delta_mrr']:+.4f}"
        )
    print()
    print(f"=== recommendation ({verdict}) ===")
    print(f"  {rec_str}")
    print()

    report = {
        "schema_version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gold_file": str(gold_path),
        "baseline": {
            "weights": baseline_run["weights"],
            "ce_blend": baseline_run["ce_blend"],
            "ndcg_at_5": baseline_ndcg,
            "mrr": baseline_mrr,
        },
        "baseline_reference": BASELINE_BASELINE,
        "top_5_configurations": top_5,
        "all_runs": [{k: v for k, v in r.items() if k != "name"} for r in runs_sorted],
        "all_runs_count": len(runs),
        "wall_time_seconds": wall_seconds,
        "verdict": verdict,
        "recommendation": rec_str,
        "recommended_config": (
            {"weights": chosen["weights"], "ce_blend": chosen["ce_blend"]}
            if verdict == "RECOMMEND_APPLY"
            else None
        ),
        "expected_ndcg_improvement": round(best_delta, 6),
    }
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"  report written: {output_path}")


if __name__ == "__main__":
    main()
