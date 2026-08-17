"""Shared benchmark observability and phase tracking utilities."""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)


def init_benchmark_stdout() -> None:
    """Ensure stdout and stderr are unbuffered for immediate live streaming."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(line_buffering=True)
        except Exception:
            pass


def print_stage_banner(stage_num: int, title: str, details: str = "") -> None:
    """Print a standardized phase/stage demarcation banner."""
    print(f"\n{'='*70}", flush=True)
    msg = f"PHASE {stage_num}: {title.upper()}"
    if details:
        msg += f" — {details}"
    print(msg, flush=True)
    print(f"{'='*70}", flush=True)


def format_query_progress(
    q_num: int,
    total_q: int,
    score: float,
    latency_ms: float,
    running_acc: float,
    category: str,
    query_text: str,
    pass_threshold: float = 0.6,
    status_icon: str | None = None,
    extra_metric_label: str = "Acc",
) -> str:
    """Format a standardized single-line query execution progress string."""
    if status_icon is None:
        status_icon = "✅ PASS" if score >= pass_threshold else "❌ FAIL"
    cat_str = (category[:24] if len(category) > 24 else category).ljust(24)
    q_preview = query_text.replace("\n", " ").strip()
    if len(q_preview) > 35:
        q_preview = q_preview[:32] + "..."

    return (
        f"  [Q {q_num:2d}/{total_q:2d}] {status_icon} (Score: {score:.2f}, {latency_ms:5.0f}ms) "
        f"| {extra_metric_label}: {running_acc*100:5.1f}% | [{cat_str}] Q: {q_preview}"
    )


def write_live_progress(
    progress_file: Path,
    q_num: int,
    total_q: int,
    category: str,
    question_text: str,
    score: float,
    latency_ms: float,
    running_overall: float,
    running_per_type: Mapping[str, float] | None = None,
    extra_fields: Mapping[str, Any] | None = None,
) -> None:
    """Write an atomic live progress JSON file for zero-latency external monitoring."""
    tmp_file = progress_file.with_suffix(".tmp")
    data: dict[str, Any] = {
        "status": "running" if q_num < total_q else "completed",
        "completed_questions": q_num,
        "total_questions": total_q,
        "percent_complete": round((q_num / max(1, total_q)) * 100, 1),
        "current_category": category,
        "current_question": question_text[:120].replace("\n", " ").strip(),
        "last_question_score": round(score, 4),
        "last_question_latency_ms": round(latency_ms, 1),
        "running_overall_accuracy": round(running_overall, 4),
        "running_per_type_accuracy": {
            k: round(v, 4) for k, v in (running_per_type or {}).items()
        },
        "last_heartbeat_epoch": time.time(),
        "last_heartbeat_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if extra_fields:
        data.update(extra_fields)

    try:
        progress_file.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp_file.replace(progress_file)
    except Exception as exc:
        logger.debug("Failed writing live progress file: %s", exc)


def print_summary_report(
    benchmark_name: str,
    total_q: int,
    wall_time_s: float,
    overall_metric: float,
    metric_name: str = "Overall Accuracy",
    category_scores: Mapping[str, float] | None = None,
    category_counts: Mapping[str, int] | None = None,
    latency_stats: Mapping[str, float] | None = None,
    retrieval_recalls: Mapping[str, float] | None = None,
    output_path: Path | None = None,
) -> None:
    """Print standardized summary report table."""
    q_rate = total_q / max(0.01, wall_time_s)
    print(f"\n{'='*70}", flush=True)
    print(f"{benchmark_name.upper()} BENCHMARK RESULTS ({total_q} questions)", flush=True)
    print(f"{'='*70}", flush=True)
    print(f"{metric_name:<25}: {overall_metric:.4f}")
    print(f"Wall Time                : {wall_time_s:.2f}s ({q_rate:.1f} queries/sec)")

    if latency_stats:
        mean_l = latency_stats.get("mean", 0.0)
        p50_l = latency_stats.get("p50", 0.0)
        p95_l = latency_stats.get("p95", 0.0)
        max_l = latency_stats.get("max", 0.0)
        print(f"Latency (mean/p50/p95/max): {mean_l:.1f}ms / {p50_l:.1f}ms / {p95_l:.1f}ms / {max_l:.1f}ms")

    if retrieval_recalls:
        print("\nRetrieval Recall Breakdown:")
        for rk, rv in retrieval_recalls.items():
            print(f"  {rk:<23}: {rv:.4f}")

    if category_scores:
        print("\nPer-Category Breakdown:")
        for cat, score in sorted(category_scores.items()):
            cnt_str = f"({category_counts.get(cat, 0)} questions)" if category_counts else ""
            print(f"  {cat:<25}: {score:.4f} {cnt_str}")

    if output_path:
        print(f"\nSaved detailed results to {output_path}", flush=True)
