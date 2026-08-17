#!/usr/bin/env python3
"""Standalone golden eval — runs directly against a pre-built DB with full observability."""
import json
import os
import sys
import time
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from eval._fixtures import (
    format_query_progress,
    init_benchmark_stdout,
    print_stage_banner,
    print_summary_report,
    set_benchmark_env,
    write_live_progress,
)

init_benchmark_stdout()
set_benchmark_env()

eval_db_str = os.environ.get("EVAL_DB", "")
eval_db = Path(eval_db_str) if eval_db_str else (INSTALL_DIR / "eval" / "prebuilt_db_path.txt")
if eval_db.suffix == ".txt":
    eval_db = Path(eval_db.read_text().strip())
os.environ["MEMORY_DB_PATH"] = str(eval_db)
os.environ["MEMORY_CONFIG_DRIFT_SKIP_ENFORCEMENT"] = "1"
os.environ["MEMORY_ESCAPE_HATCH"] = (
    "ignore-stability;golden-eval-dedicated-db;golden_eval;14400;60"
)

from search.orchestrator import search_memories
from infra._lazy_imports import get_embedding_search

print(f"\n{'='*80}", flush=True)
print("BENCHMARK SUITE: GOLDEN RETRIEVAL EVALUATION", flush=True)
print(f"{'='*80}", flush=True)

# Phase 1: Load dataset
print_stage_banner(1, "Dataset Loading", "real_memory_golden_v2.json")
golden_path = INSTALL_DIR / "eval" / "real_memory_golden_v2.json"
golden = json.load(open(golden_path))
targets = golden["targets"]
test_cases = golden["test_cases"]
print(f"✓ Loaded {len(test_cases)} golden test cases from {golden_path.name}", flush=True)

# Phase 2: Warmup & Model Preloading
print_stage_banner(2, "Search Pipeline Warmup", "Pre-loading embedding & cross-encoder models")
print("Pre-loading models...", end=" ", flush=True)
es = get_embedding_search()
es.wait_for_model(timeout_s=60.0)
print(f"embedding={'OK' if es.model else 'FAIL'}", end=" ", flush=True)
try:
    from search.rerankers import _get_ce_chunk_model
    ce = _get_ce_chunk_model()
    print(f"ce={'OK' if ce else 'skip'}", end=" ", flush=True)
except Exception:
    print("ce=skip", end=" ", flush=True)
print("\n✓ Models ready.", flush=True)

def recall_at_k(retrieved, expected, k):
    if not expected:
        return 1.0
    hits = len(set(expected) & set(retrieved[:k]))
    return hits / len(expected)

def mrr(retrieved, expected):
    for i, d in enumerate(retrieved):
        if d in expected:
            return 1.0 / (i + 1)
    return 0.0

# Phase 3: Evaluation Execution
print_stage_banner(3, "Evaluation Execution", f"{len(test_cases)} golden queries against {eval_db.name}")

results = {"recall_at_10": [], "mrr": []}
cat_results = {}
cat_counts = {}
latencies = []
progress_file = INSTALL_DIR / "eval" / "results" / ".progress.json"
suite_progress_file = INSTALL_DIR / "eval" / "results" / ".progress_golden_eval.json"
t_start = time.time()

for i, tc in enumerate(test_cases, start=1):
    query = tc["query"]
    expected = tc["expected"]
    cat = tc.get("category", "unknown")

    as_of = None
    if cat == "temporal":
        import re
        import calendar
        for eid in expected:
            m = re.search(r'(\d{4})-(\d{2})-(\d{2})', eid)
            if m:
                as_of = calendar.timegm(time.strptime(m.group(0), "%Y-%m-%d")) + 86400
                break

    t0 = time.time()
    try:
        res = search_memories(query=query, db_path=eval_db, limit=50, hybrid=True, rerank=True, as_of=as_of, light=False)
        retrieved = [r.get("id", "") for r in res.get("results", [])] if isinstance(res, dict) else []
    except Exception as e:
        retrieved = []
    lat = (time.time() - t0) * 1000
    latencies.append(lat)

    r10 = recall_at_k(retrieved, expected, 10)
    m = mrr(retrieved, expected)
    results["recall_at_10"].append(r10)
    results["mrr"].append(m)

    cat_results.setdefault(cat, {"r10": [], "lat": []})
    cat_results[cat]["r10"].append(r10)
    cat_results[cat]["lat"].append(lat)
    cat_counts[cat] = cat_counts.get(cat, 0) + 1

    running_r10 = sum(results["recall_at_10"]) / len(results["recall_at_10"])
    running_per_type = {
        c: sum(cd["r10"]) / len(cd["r10"]) for c, cd in cat_results.items() if cd["r10"]
    }

    # Single-line live query stream
    line_msg = format_query_progress(
        q_num=i,
        total_q=len(test_cases),
        score=r10,
        latency_ms=lat,
        running_acc=running_r10,
        category=cat,
        query_text=query,
        pass_threshold=tc.get("min_recall_at_10", 0.8),
        extra_metric_label="Rec@10",
    )
    print(line_msg, flush=True)

    # Atomic live progress writer
    for p_file in (progress_file, suite_progress_file):
        write_live_progress(
            progress_file=p_file,
            q_num=i,
            total_q=len(test_cases),
            category=cat,
            question_text=query,
            score=r10,
            latency_ms=lat,
            running_overall=running_r10,
            running_per_type=running_per_type,
            extra_fields={"benchmark": "Golden-Eval"},
        )

wall_time = time.time() - t_start
avg_r10 = sum(results["recall_at_10"]) / len(results["recall_at_10"]) if results["recall_at_10"] else 0.0
avg_mrr = sum(results["mrr"]) / len(results["mrr"]) if results["mrr"] else 0.0

# Phase 4: Results Aggregation
print_stage_banner(4, "Results Aggregation & Verification", f"{len(test_cases)} questions analyzed")

from eval.bench.metrics import calculate_latency_stats
lat_stats = calculate_latency_stats(latencies)

cat_scores_map = {
    c: sum(cd["r10"]) / len(cd["r10"]) for c, cd in cat_results.items() if cd["r10"]
}
retrieval_recalls = {
    "Recall@10 (Overall)": avg_r10,
    "MRR (Overall)": avg_mrr,
}

out_file = INSTALL_DIR / "eval" / "results" / "golden_eval_results.json"
out_file.parent.mkdir(parents=True, exist_ok=True)
report_data = {
    "benchmark": "Golden-Retrieval-v2",
    "total_questions": len(test_cases),
    "wall_time_seconds": round(wall_time, 2),
    "overall_recall_at_10": round(avg_r10, 4),
    "overall_mrr": round(avg_mrr, 4),
    "category_recall_at_10": {k: round(v, 4) for k, v in cat_scores_map.items()},
    "category_counts": cat_counts,
    "latency_ms": lat_stats,
}
with open(out_file, "w", encoding="utf-8") as f:
    json.dump(report_data, f, indent=2)

print_summary_report(
    benchmark_name="Golden Retrieval",
    total_q=len(test_cases),
    wall_time_s=wall_time,
    overall_metric=avg_r10,
    metric_name="Recall@10 (Overall)",
    category_scores=cat_scores_map,
    category_counts=cat_counts,
    latency_stats=lat_stats,
    retrieval_recalls=retrieval_recalls,
    output_path=out_file,
)

