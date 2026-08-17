"""Unified Benchmarking Framework for agentic-memory.

Provides standardized datasets, pre-indexed DB caching, metrics,
and multi-suite execution for LoCoMo, LongMemEval, BEAM, Adversarial, and Golden sets.
"""

from __future__ import annotations

from .protocol import (
    BenchmarkSession,
    BenchmarkQuestion,
    BenchmarkResult,
    SuiteSummary,
)
from .metrics import (
    compute_retrieval_metrics,
    compute_text_metrics,
    compute_lafs,
    calculate_latency_stats,
)
from .db_manager import BenchmarkDBManager
from .engine import BenchmarkHarness

from .observability import (
    init_benchmark_stdout,
    print_stage_banner,
    format_query_progress,
    write_live_progress,
    print_summary_report,
)

__all__ = [
    "BenchmarkSession",
    "BenchmarkQuestion",
    "BenchmarkResult",
    "SuiteSummary",
    "compute_retrieval_metrics",
    "compute_text_metrics",
    "compute_lafs",
    "calculate_latency_stats",
    "BenchmarkDBManager",
    "BenchmarkHarness",
    "init_benchmark_stdout",
    "print_stage_banner",
    "format_query_progress",
    "write_live_progress",
    "print_summary_report",
]

