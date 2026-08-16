"""Universal benchmark execution engine for agentic-memory evaluation."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .adapters import ADAPTERS
from .adapters.base import BaseBenchmarkAdapter
from .db_manager import BenchmarkDBManager
from .metrics import (
    calculate_latency_stats,
    compute_lafs,
    compute_retrieval_metrics,
    compute_text_metrics,
)
from .protocol import BenchmarkQuestion, BenchmarkResult, BenchmarkSession, SuiteSummary

logger = logging.getLogger(__name__)

BENCH_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS_DIR = BENCH_ROOT / "results"


def set_benchmark_env() -> None:
    """Set optimal environment variables for single-process evaluation."""
    os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"
    os.environ["MEMORY_FAIL_ON_INTEGRITY_DRIFT"] = "0"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"


class BenchmarkHarness:
    """Central harness that runs evaluation queries against the 14-phase search orchestrator."""

    def __init__(
        self,
        results_dir: Path | None = None,
        db_manager: BenchmarkDBManager | None = None,
    ) -> None:
        self.results_dir = results_dir or DEFAULT_RESULTS_DIR
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.db_manager = db_manager or BenchmarkDBManager()

    def cleanup(self) -> None:
        """Clean up temporary resources and non-cached database directories."""
        self.db_manager.cleanup_temp_dirs()

    def run_suite(
        self,
        suite_name: str,
        adapter_or_name: str | BaseBenchmarkAdapter,
        max_questions: int | None = None,
        use_cache_db: bool = True,
        force_rebuild: bool = False,
        checkpoint_interval: int = 50,
        config: dict[str, Any] | None = None,
    ) -> SuiteSummary:
        """Run a full benchmark suite end-to-end."""
        set_benchmark_env()

        # 1. Resolve adapter
        if isinstance(adapter_or_name, str):
            if adapter_or_name not in ADAPTERS:
                raise ValueError(f"Unknown adapter '{adapter_or_name}'. Available: {list(ADAPTERS.keys())}")
            adapter = ADAPTERS[adapter_or_name]()
        else:
            adapter = adapter_or_name

        print(f"\n{'='*70}")
        print(f"BENCHMARK SUITE: {suite_name.upper()} (v{adapter.version})")
        print(f"{'='*70}")

        # 2. Load dataset with error handling
        print(f"Loading dataset via {adapter.__class__.__name__}...")
        t_load = time.time()
        try:
            sessions, questions = adapter.load(limit=max_questions)
            print(f"Loaded {len(sessions)} sessions, {len(questions)} questions in {time.time() - t_load:.2f}s")
        except Exception as exc:
            err_msg = f"Failed to load dataset in adapter {adapter.__class__.__name__}: {exc}\n{traceback.format_exc()}"
            logger.error(err_msg)
            print(f"ERROR: {err_msg}")
            return SuiteSummary(
                suite_name=suite_name,
                dataset_version=adapter.version,
                total_questions=0,
                total_sessions_ingested=0,
                ingest_time_seconds=0.0,
                wall_time_seconds=round(time.time() - t_load, 2),
                latency_ms={},
                macro_metrics={},
                category_metrics={},
                error=err_msg,
            )

        if not questions:
            print("WARNING: No questions to evaluate.")
            return SuiteSummary(
                suite_name=suite_name,
                dataset_version=adapter.version,
                total_questions=0,
                total_sessions_ingested=len(sessions),
                ingest_time_seconds=0.0,
                wall_time_seconds=0.0,
                latency_ms={},
                macro_metrics={},
                category_metrics={},
            )

        # 3. Get or build database
        print(f"Preparing database for {suite_name} (cache={use_cache_db}, rebuild={force_rebuild})...")
        db_path, ingest_time, was_cached = self.db_manager.get_or_create_db(
            suite_name=suite_name,
            sessions=sessions,
            tenant_id=adapter.tenant_id,
            use_cache=use_cache_db,
            force_rebuild=force_rebuild,
        )
        os.environ["MEMORY_DB_PATH"] = str(db_path)
        print(f"Database ready at {db_path} (cached={was_cached}, ingest_time={ingest_time:.2f}s)")

        # 4. Run evaluation loop
        print(f"\nExecuting {len(questions)} queries against 14-phase search pipeline...")
        from search.orchestrator import search_memories
        import memory_mcp

        results: list[BenchmarkResult] = []
        latencies: list[float] = []
        per_category_scores: dict[str, list[dict[str, float]]] = {}

        t_start_wall = time.time()
        read_conn = sqlite3.connect(str(db_path), timeout=30.0)

        checkpoint_path = self.results_dir / f"{suite_name}_checkpoint.json"

        try:
            total_q = len(questions)
            for q_idx, q in enumerate(questions, start=1):
                # Progress logging with live ETA calculation
                if q_idx % 25 == 0 or q_idx == 1 or q_idx == total_q:
                    elapsed = time.time() - t_start_wall
                    q_rate = q_idx / max(0.1, elapsed)
                    remaining_q = total_q - q_idx
                    eta_sec = remaining_q / max(0.01, q_rate)
                    print(
                        f"  [{q_idx}/{total_q}] elapsed={elapsed:.1f}s, ETA={eta_sec:.1f}s ({q_rate:.1f} q/s)...",
                        flush=True,
                    )

                if hasattr(memory_mcp, "_search_cache"):
                    memory_mcp._search_cache.clear()

                t0 = time.perf_counter()
                search_res = search_memories(
                    db_path,
                    q.query,
                    limit=50,
                    include_global=True,
                    rerank=True,
                    deep_rerank=True,
                    tenant_id=adapter.tenant_id,
                    as_of=q.as_of,
                    category="all",
                )
                latency_ms = (time.perf_counter() - t0) * 1000.0
                latencies.append(latency_ms)

                retrieved_items = search_res.get("results", [])
                retrieved_ids = [r["id"] for r in retrieved_items]

                # Safe ID-to-content lookup avoiding zip misalignment
                top10_ids = retrieved_ids[:10]
                if top10_ids:
                    placeholders = ",".join("?" for _ in top10_ids)
                    rows = read_conn.execute(
                        f"SELECT id, content FROM memories WHERE id IN ({placeholders})",
                        tuple(top10_ids),
                    ).fetchall()
                    id_to_content = {r[0]: r[1] for r in rows}
                else:
                    id_to_content = {}

                retrieved_contents = [id_to_content.get(mid, "") for mid in top10_ids if mid in id_to_content]
                combined_content = " ".join(retrieved_contents)
                candidates_tuple = [(mid, id_to_content.get(mid, ""), "", "", "") for mid in top10_ids]

                # Solver phases (Math, Temporal Delta, Attribute Extraction)
                try:
                    from search.phases.math_aggregator import extract_and_aggregate_quantities
                    math_sum = extract_and_aggregate_quantities(q.query, candidates_tuple)
                    if math_sum:
                        combined_content = f"{math_sum} " + combined_content
                except Exception as exc:
                    logger.debug("Math phase failed (non-fatal): %s", exc)

                try:
                    from search.phases.temporal_delta_solver import calculate_temporal_delta
                    temp_delta = calculate_temporal_delta(q.query, candidates_tuple)
                    if temp_delta:
                        combined_content = f"{temp_delta} " + combined_content
                except Exception as exc:
                    logger.debug("Temporal delta solver failed (non-fatal): %s", exc)

                try:
                    from search.phases.attribute_extractor import extract_entity_attribute
                    attr_val = extract_entity_attribute(q.query, candidates_tuple)
                    if attr_val:
                        combined_content = f"{attr_val} " + combined_content
                except Exception as exc:
                    logger.debug("Attribute extractor failed (non-fatal): %s", exc)

                # Compute scores
                scores: dict[str, float] = {}
                if q.gold_session_ids:
                    r_metrics = compute_retrieval_metrics(retrieved_ids, q.gold_session_ids)
                    scores.update(r_metrics)

                if q.expected_answer or q.rubric or q.compliance_indicators:
                    t_metrics = compute_text_metrics(
                        combined_content,
                        q.expected_answer or "",
                        rubric=q.rubric,
                        compliance_indicators=q.compliance_indicators,
                    )
                    scores.update(t_metrics)
                    scores["lafs"] = compute_lafs(t_metrics.get("token_f1", 0.0), latency_ms)

                # Parse search envelope telemetry
                phase_latencies = search_res.get("phase_latencies", {})
                phase_errors_raw = search_res.get("phase_errors", {})
                if isinstance(phase_errors_raw, dict):
                    phase_errors = [f"{k}:{v}" for k, v in phase_errors_raw.items()]
                elif isinstance(phase_errors_raw, list):
                    phase_errors = [str(x) for x in phase_errors_raw]
                else:
                    phase_errors = []

                res = BenchmarkResult(
                    question_id=q.question_id,
                    category=q.category,
                    query=q.query,
                    expected=q.expected_answer,
                    retrieved_ids=retrieved_ids[:20],
                    retrieved_content=retrieved_contents[:5],
                    scores=scores,
                    latency_ms=round(latency_ms, 2),
                    phases=list(phase_latencies.keys()),
                    phase_latencies=phase_latencies,
                    phase_errors=phase_errors,
                )
                results.append(res)
                per_category_scores.setdefault(q.category, []).append(scores)

                # Periodic checkpointing
                if checkpoint_interval > 0 and q_idx % checkpoint_interval == 0:
                    try:
                        with open(checkpoint_path, "w", encoding="utf-8") as f:
                            json.dump(
                                {
                                    "suite_name": suite_name,
                                    "completed": q_idx,
                                    "total": total_q,
                                    "results": [asdict(r) for r in results],
                                },
                                f,
                                indent=2,
                            )
                    except Exception as exc:
                        logger.debug("Failed writing checkpoint: %s", exc)

        finally:
            read_conn.close()
            # Clean up checkpoint on completion
            if checkpoint_path.exists():
                try:
                    checkpoint_path.unlink(missing_ok=True)
                except OSError:
                    pass

        wall_time = time.time() - t_start_wall
        latency_stats = calculate_latency_stats(latencies)

        # 5. Compute macro aggregations
        all_metric_keys = set()
        for r in results:
            all_metric_keys.update(r.scores.keys())

        macro_metrics: dict[str, float] = {}
        for k in sorted(all_metric_keys):
            vals = [r.scores[k] for r in results if k in r.scores]
            macro_metrics[k] = round(sum(vals) / len(vals), 4) if vals else 0.0

        category_metrics: dict[str, dict[str, float]] = {}
        for cat, score_dicts in per_category_scores.items():
            cat_summary: dict[str, float] = {}
            for k in sorted(all_metric_keys):
                vals = [sd[k] for sd in score_dicts if k in sd]
                if vals:
                    cat_summary[k] = round(sum(vals) / len(vals), 4)
            cat_summary["count"] = len(score_dicts)
            category_metrics[cat] = cat_summary

        summary = SuiteSummary(
            suite_name=suite_name,
            dataset_version=adapter.version,
            total_questions=len(results),
            total_sessions_ingested=len(sessions),
            ingest_time_seconds=ingest_time,
            wall_time_seconds=round(wall_time, 2),
            latency_ms=latency_stats,
            macro_metrics=macro_metrics,
            category_metrics=category_metrics,
            results=results,
            config=config or {},
        )

        # 6. Save results
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_file = self.results_dir / f"{suite_name}_{ts_str}.json"
        latest_file = self.results_dir / f"latest_{suite_name}.json"

        summary_dict = {
            "suite_name": summary.suite_name,
            "dataset_version": summary.dataset_version,
            "total_questions": summary.total_questions,
            "total_sessions_ingested": summary.total_sessions_ingested,
            "ingest_time_seconds": summary.ingest_time_seconds,
            "wall_time_seconds": summary.wall_time_seconds,
            "latency_ms": summary.latency_ms,
            "macro_metrics": summary.macro_metrics,
            "category_metrics": summary.category_metrics,
            "results": [asdict(r) for r in summary.results],
            "config": summary.config,
            "error": summary.error,
        }

        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(summary_dict, f, indent=2)
        with open(latest_file, "w", encoding="utf-8") as f:
            json.dump(summary_dict, f, indent=2)

        # 7. Print summary report
        print(f"\n{'='*70}")
        print(f"RESULTS SUMMARY: {suite_name.upper()}")
        print(f"{'='*70}")
        print(f"Total Questions: {summary.total_questions}")
        print(f"Wall Time:       {summary.wall_time_seconds}s ({summary.total_questions / max(0.1, summary.wall_time_seconds):.1f} q/s)")
        print(f"Latency:         mean={latency_stats['mean']}ms, p50={latency_stats['p50']}ms, p95={latency_stats['p95']}ms, p99={latency_stats['p99']}ms")
        print("\nMacro Metrics:")
        for mk, mv in summary.macro_metrics.items():
            print(f"  {mk:<20}: {mv:.4f}")

        print("\nCategory Metrics:")
        for cat, cm in sorted(summary.category_metrics.items()):
            n_cat = int(cm.get("count", 0))
            acc = cm.get("overall_accuracy", cm.get("recall@10", cm.get("exact_match", 0.0)))
            print(f"  {cat:<25} (n={n_cat:>4}): score={acc:.4f}")

        print(f"\nSaved detailed results to:\n  - {out_file}\n  - {latest_file}")
        return summary
