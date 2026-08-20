"""Retrieval benchmark harness for the agentic-memory search pipeline.

Measures precision@k, recall@k, MRR, and phase latency against a
golden dataset loaded from ``retrieval_golden_set.json``.

Usage::

    bench = RetrievalBenchmark()
    report = bench.run()
    print(report["phases"]["fts"]["precision_at_5"])

The golden dataset JSON lives alongside this module and is loaded at
construction time.  Each test case specifies an expected set of note IDs
that *must* appear in the top-k results for the query to be considered a
hit.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Bootstrap — mirrors eval/test_search_pipeline_unit.py convention
# ---------------------------------------------------------------------------
INSTALL_DIR = Path(__file__).resolve().parent.parent
if str(INSTALL_DIR) not in sys.path:
    sys.path.insert(0, str(INSTALL_DIR))

from infra.memory_common import open_db  # noqa: E402
from infra.memory_config import get_memory_paths  # noqa: E402
_, _, GLOBAL_MEM_DIR = get_memory_paths()
from _fixtures import (
    bootstrap_temp_db_clean,
    format_query_progress,
    init_benchmark_stdout,
    print_stage_banner,
    print_summary_report,
    set_benchmark_env,
    write_live_progress,
)

init_benchmark_stdout()
set_benchmark_env()


# ---------------------------------------------------------------------------
# Golden dataset types
# ---------------------------------------------------------------------------

_MEMORY_ENTRY = dict[str, Any]
_TEST_CASE_ENTRY = dict[str, Any]
_GOLDEN_SET = dict[str, Any]


@dataclass
class RetrievalTestCase:
    """One search evaluation instance.

    Attributes:
        query:            Natural-language query string.
        expected_note_ids: Ground-truth note IDs that *must* appear in
                           the top-k results for the case to count as a hit.
        category:         Optional category filter (empty string = any).
        tags:             Optional tag filter (None = any).
        min_precision_at_5: Pass threshold for precision@5 (informational;
                            actual assertion is done in the test file).
        min_recall_at_5:    Pass threshold for recall@5 (informational).
    """

    query: str
    expected_note_ids: set[str]
    category: str = ""
    tags: list[str] | None = None
    min_precision_at_5: float = 0.4
    min_recall_at_5: float = 0.4


@dataclass
class PhaseMetrics:
    """Aggregated metrics for one search phase."""

    precision_at_5: float = 0.0
    recall_at_5: float = 0.0
    precision_at_10: float = 0.0
    recall_at_10: float = 0.0
    mrr: float = 0.0
    latency_ms: float = 0.0
    total_cases: int = 0
    hits_at_5: int = 0
    hits_at_10: int = 0


# ---------------------------------------------------------------------------
# Golden dataset loader
# ---------------------------------------------------------------------------

_GOLDEN_DIR = Path(__file__).resolve().parent
_GOLDEN_PATH = _GOLDEN_DIR / "retrieval_golden_set.json"


def _load_golden_set() -> _GOLDEN_SET:
    """Load the golden dataset from the JSON file next to this module."""
    with open(_GOLDEN_PATH, encoding="utf-8") as fh:
        raw: dict[str, Any] = json.load(fh)
    return raw


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_PROD_DB = Path(
    os.environ.get(
        "MEMORY_DB_PATH",
        str(GLOBAL_MEM_DIR / "memory.db"),
    )
)


def _now_iso() -> str:
    """Return a fixed UTC timestamp so golden-set rows are deterministic."""
    from datetime import datetime, timezone

    return datetime(2024, 1, 1, tzinfo=timezone.utc).isoformat()


def _insert_memory(
    db_path: Path,
    note_id: str,
    category: str,
    tags: list[str],
    content: str,
) -> None:
    """Insert one memory row directly into the DB (no .md side-effect).

    Also inserts into ``memories_fts`` so the FTS5 index is immediately
    searchable — required so that the golden-set queries match.
    """
    cat, slug = (note_id.split("/", 1) + [""])[:2]
    cat = cat or category
    tags_str = json.dumps(tags)
    now = _now_iso()
    source_file = f"bench/{cat}/{slug}"
    with open_db(db_path, pooled=False) as db:
        db.execute(
            """
            INSERT OR REPLACE INTO memories
                (id, content, source_file, tags, created_at, updated_at,
                 observed_at, pinned, importance, category, repo_id,
                 access_count, success_score, fitness_score, tenant_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 3, ?, ?, 1, 0.0, 1.0, 'default')
            """,
            (
                note_id,
                content,
                source_file,
                tags_str,
                now,
                now,
                now,
                cat,
                source_file,
            ),
        )
        rowid = db.execute(
            "SELECT rowid FROM memories WHERE id=?", (note_id,)
        ).fetchone()
        if rowid is not None:
            try:
                db.execute(
                    "INSERT OR REPLACE INTO memories_fts(rowid, id, content, tags, category) VALUES (?, ?, ?, ?, ?)",
                    (rowid[0], note_id, content, tags_str, cat),
                )
            except Exception:
                pass
        db.commit()


def _seed_db(db_path: Path, golden: _GOLDEN_SET) -> None:
    """Insert all memories from *golden* into *db_path* and build all indexes."""
    items: list[tuple[str, str, str, list[str] | None]] = []
    for mem in golden.get("memories", []):
        _insert_memory(
            db_path=db_path,
            note_id=mem["note_id"],
            category=mem.get("category", "lessons"),
            tags=mem.get("tags", []),
            content=mem.get("content", ""),
        )
        items.append((mem["note_id"], mem.get("content", ""), "", None))
    if items:
        from _fixtures import populate_eval_memory_indexes_batch

        conn = sqlite3.connect(str(db_path), timeout=30.0)
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            populate_eval_memory_indexes_batch(conn, items, use_llm_facts=False)
        finally:
            conn.close()

        try:
            from rebuild_vec_index import rebuild_vec_index
            stats = rebuild_vec_index(str(db_path))
            print(
                f"✓ Vector index built: {stats.get('n_indexed')} items ({stats.get('serialized_bytes')} bytes) in {stats.get('elapsed_s', 0.0):.2f}s",
                flush=True,
            )
        except Exception:
            pass



# ---------------------------------------------------------------------------
# Metrics helpers
# ---------------------------------------------------------------------------

def _precision_at_k(retrieved: list[str], expected: set[str], k: int) -> float:
    top_k = retrieved[:k]
    if not top_k:
        return 0.0
    return len(set(top_k) & expected) / len(top_k)


def _recall_at_k(retrieved: list[str], expected: set[str], k: int) -> float:
    if not expected:
        return 1.0
    top_k = retrieved[:k]
    return len(set(top_k) & expected) / len(expected)


def _mrr(retrieved: list[str], expected: set[str]) -> float:
    for rank, nid in enumerate(retrieved, start=1):
        if nid in expected:
            return 1.0 / rank
    return 0.0


# ---------------------------------------------------------------------------
# Phase runner record
# ---------------------------------------------------------------------------

@dataclass
class _PhaseRunResult:
    """Metrics accumulated over all cases for one phase."""

    name: str
    total_cases: int = 0
    hits_at_5: int = 0
    hits_at_10: int = 0
    prec_at_5_sum: float = 0.0
    prec_at_10_sum: float = 0.0
    rec_at_5_sum: float = 0.0
    rec_at_10_sum: float = 0.0
    mrr_sum: float = 0.0
    latency_sum_ms: float = 0.0

    def record(self, prec5: float, rec5: float, prec10: float, rec10: float,
               mrr_val: float, latency_ms: float) -> None:
        self.total_cases += 1
        if rec5 >= 1.0:
            self.hits_at_5 += 1
        if rec10 >= 1.0:
            self.hits_at_10 += 1
        self.prec_at_5_sum += prec5
        self.prec_at_10_sum += prec10
        self.rec_at_5_sum += rec5
        self.rec_at_10_sum += rec10
        self.mrr_sum += mrr_val
        self.latency_sum_ms += latency_ms

    def to_metrics(self) -> PhaseMetrics:
        n = self.total_cases or 1
        return PhaseMetrics(
            precision_at_5=round(self.prec_at_5_sum / n, 4),
            recall_at_5=round(self.rec_at_5_sum / n, 4),
            precision_at_10=round(self.prec_at_10_sum / n, 4),
            recall_at_10=round(self.rec_at_10_sum / n, 4),
            mrr=round(self.mrr_sum / n, 4),
            latency_ms=round(self.latency_sum_ms / n, 2),
            total_cases=self.total_cases,
            hits_at_5=self.hits_at_5,
            hits_at_10=self.hits_at_10,
        )


# ---------------------------------------------------------------------------
# Benchmark class
# ---------------------------------------------------------------------------


class RetrievalBenchmark:
    """End-to-end retrieval benchmark backed by a golden dataset.

    Creates a fresh temp DB for every :meth:`run` call, seeds it with the
    golden memories, then executes each test case across the requested
    search phases.

    Phase runner convention
    -----------------------
    Each runner is a callable::

        runner(query, category, tags, limit) -> result_dict

    where ``result_dict`` is the return value of ``search_memories``.
    """

    def __init__(self, golden_path: Path | None = None) -> None:
        self._golden: _GOLDEN_SET = _load_golden_set()
        self._cases: list[RetrievalTestCase] = [
            RetrievalTestCase(
                query=tc["query"],
                expected_note_ids=set(tc["expected_note_ids"]),
                category=tc.get("category", ""),
                tags=tc.get("tags"),
                min_precision_at_5=tc.get("min_precision_at_5", 0.4),
                min_recall_at_5=tc.get("min_recall_at_5", 0.4),
            )
            for tc in self._golden.get("test_cases", [])
        ]
        self._tmpdir: Path | None = None

    # ------------------------------------------------------------------
    # DB lifecycle
    # ------------------------------------------------------------------

    def _setup_db(self) -> Path:
        """Bootstrap a fresh temp DB and seed it with golden memories."""
        self._tmpdir = Path(tempfile.mkdtemp(prefix="retrieval_bench_"))
        db_path = self._tmpdir / "memory.db"
        os.environ["MEMORY_DB_PATH"] = str(db_path)
        # Benchmarks deliberately point MEMORY_DB_PATH at a throwaway DB;
        # arm the config-drift escape hatch (audited, time-bounded).
        os.environ["MEMORY_ESCAPE_HATCH"] = (
            "ignore-stability;retrieval-benchmark-temp-db;retrieval_bench;14400;60"
        )
        bootstrap_temp_db_clean(db_path)
        _seed_db(db_path, self._golden)
        return db_path

    def _teardown_db(self) -> None:
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    # ------------------------------------------------------------------
    # Phase runners — imported lazily so existing module-level imports
    # in orchestrator.py are not evaluated at module load time.
    # ------------------------------------------------------------------

    def _run_fts(
        self,
        db_path: Path,
        query: str,
        category: str,
        tags: list[str] | None,
        limit: int = 10,
    ) -> dict:
        """Run FTS-only search (rerank disabled, light mode, no facts)."""
        from search.orchestrator import search_memories

        return search_memories(
            db_path,
            query,
            limit=limit,
            category=category,
            tags=tags,
            include_global=True,
            rerank=False,
            light=True,
            include_facts=False,
            safety_wiring=False,
            tenant_id="bench",
        )

    def _run_hybrid(
        self,
        db_path: Path,
        query: str,
        category: str,
        tags: list[str] | None,
        limit: int = 10,
    ) -> dict:
        """Run full hybrid search (default settings, no rerank for fairness)."""
        from search.orchestrator import search_memories

        return search_memories(
            db_path,
            query,
            limit=limit,
            category=category or "",
            tags=tags,
            include_global=True,
            rerank=False,
            include_facts=False,
            safety_wiring=False,
            tenant_id="bench",
        )

    # ------------------------------------------------------------------
    # Core runner
    # ------------------------------------------------------------------

    def _run_case(
        self,
        db_path: Path,
        case: RetrievalTestCase,
        run_fn: Any,
        accumulator: _PhaseRunResult,
    ) -> None:
        """Execute one test case against *run_fn* and record metrics."""
        # L10 fix: wrap search in a timeout to prevent hung calls
        import concurrent.futures
        t0 = time.time()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    run_fn,
                    db_path=db_path,
                    query=case.query,
                    category=case.category,
                    tags=case.tags,
                )
                result = future.result(timeout=60.0)
        except concurrent.futures.TimeoutError:
            result = {"results": [], "count": 0, "error": "search timed out after 60s"}
        latency_ms = (time.time() - t0) * 1000.0

        retrieved = [r["id"] for r in result.get("results", [])]
        expected = case.expected_note_ids

        prec5 = _precision_at_k(retrieved, expected, 5)
        rec5 = _recall_at_k(retrieved, expected, 5)
        prec10 = _precision_at_k(retrieved, expected, 10)
        rec10 = _recall_at_k(retrieved, expected, 10)
        mrr_val = _mrr(retrieved, expected)
        accumulator.record(prec5, rec5, prec10, rec10, mrr_val, latency_ms)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _warmup_models(self) -> None:
        """Pre-load embedding and reranker models so cold-start
        latency doesn't contaminate the timed measurements."""
        try:
            from infra.embedding_search import get_embedding_search

            es = get_embedding_search()
            if es.model is not None:
                es.model.encode(["warmup"])
        except Exception:
            pass
        try:
            from search.rerankers import _get_ce_chunk_model

            _get_ce_chunk_model()
        except Exception:
            pass

    def run(self) -> dict:
        """Execute the benchmark and return the full report dict."""
        print(f"\n{'='*80}", flush=True)
        print("BENCHMARK SUITE: RETRIEVAL BENCHMARK (FTS & Hybrid)", flush=True)
        print(f"{'='*80}", flush=True)

        print_stage_banner(1, "Dataset Loading", f"{len(self._golden.get('memories', []))} memories, {len(self._cases)} test cases")
        print(f"✓ Loaded {len(self._golden.get('memories', []))} golden memories and {len(self._cases)} test cases", flush=True)

        print_stage_banner(2, "Database Ingestion & Multi-Index Building", "Isolated temporary DB")
        t_setup = time.time()
        db_path = self._setup_db()
        print(f"✓ DB initialized and multi-indexed in {time.time() - t_setup:.2f}s", flush=True)

        try:
            print_stage_banner(3, "Search Pipeline Warmup", "Pre-warming models")
            self._warmup_models()
            print("✓ Encoders pre-warmed successfully.", flush=True)

            print_stage_banner(4, "Evaluation Execution", f"{len(self._cases)} test cases across FTS and Hybrid")
            phases: dict[str, _PhaseRunResult] = {
                "fts": _PhaseRunResult(name="fts"),
                "hybrid": _PhaseRunResult(name="hybrid"),
            }
            per_case: list[dict] = []
            progress_file = INSTALL_DIR / "eval" / "results" / ".progress.json"
            suite_progress_file = INSTALL_DIR / "eval" / "results" / ".progress_retrieval_bench.json"
            t_eval_start = time.time()
            all_hybrid_latencies: list[float] = []

            for idx, case in enumerate(self._cases, start=1):
                for phase_name, run_fn in [
                    ("fts", self._run_fts),
                    ("hybrid", self._run_hybrid),
                ]:
                    self._run_case(db_path, case, run_fn, phases[phase_name])

                # Get latest recorded hybrid score & latency
                h_metrics = phases["hybrid"].to_metrics()
                h_prec5 = phases["hybrid"].prec_at_5_sum / max(1, phases["hybrid"].total_cases)
                h_rec5 = phases["hybrid"].rec_at_5_sum / max(1, phases["hybrid"].total_cases)
                last_lat = phases["hybrid"].latency_sum_ms / max(1, phases["hybrid"].total_cases)
                all_hybrid_latencies.append(last_lat)

                per_case.append(
                    {
                        "query": case.query,
                        "category": case.category,
                        "tags": case.tags,
                        "expected_ids": sorted(case.expected_note_ids),
                        "min_precision_at_5": case.min_precision_at_5,
                        "min_recall_at_5": case.min_recall_at_5,
                    }
                )

                line_msg = format_query_progress(
                    q_num=idx,
                    total_q=len(self._cases),
                    score=h_rec5,
                    latency_ms=last_lat,
                    running_acc=h_rec5,
                    category=case.category or "general",
                    query_text=case.query,
                    extra_metric_label="Rec@5",
                )
                print(line_msg, flush=True)

                for p_file in (progress_file, suite_progress_file):
                    write_live_progress(
                        progress_file=p_file,
                        q_num=idx,
                        total_q=len(self._cases),
                        category=case.category or "general",
                        question_text=case.query,
                        score=h_rec5,
                        latency_ms=last_lat,
                        running_overall=h_rec5,
                        extra_fields={"benchmark": "Retrieval-Golden-Set"},
                    )

            wall_time = time.time() - t_eval_start

            print_stage_banner(5, "Results Aggregation & Verification", f"{len(self._cases)} test cases analyzed")

            report = {
                "phases": {
                    name: {
                        "precision_at_5": m.to_metrics().precision_at_5,
                        "recall_at_5": m.to_metrics().recall_at_5,
                        "precision_at_10": m.to_metrics().precision_at_10,
                        "recall_at_10": m.to_metrics().recall_at_10,
                        "mrr": m.to_metrics().mrr,
                        "latency_ms": m.to_metrics().latency_ms,
                        "total_cases": m.to_metrics().total_cases,
                        "hits_at_5": m.to_metrics().hits_at_5,
                        "hits_at_10": m.to_metrics().hits_at_10,
                    }
                    for name, m in phases.items()
                },
                "per_case": per_case,
                "dataset_info": {
                    "total_memories": len(self._golden.get("memories", [])),
                    "total_test_cases": len(self._cases),
                },
                "wall_time_seconds": round(wall_time, 2),
            }

            from eval.bench.metrics import calculate_latency_stats
            lat_stats = calculate_latency_stats(all_hybrid_latencies)

            hybrid_rec5 = report["phases"]["hybrid"]["recall_at_5"]
            retrieval_recalls = {
                "Hybrid Recall@5": hybrid_rec5,
                "Hybrid Precision@5": report["phases"]["hybrid"]["precision_at_5"],
                "FTS Recall@5": report["phases"]["fts"]["recall_at_5"],
                "FTS Precision@5": report["phases"]["fts"]["precision_at_5"],
                "Hybrid MRR": report["phases"]["hybrid"]["mrr"],
                "FTS MRR": report["phases"]["fts"]["mrr"],
            }

            out_file = INSTALL_DIR / "eval" / "results" / "retrieval_benchmark_results.json"
            out_file.parent.mkdir(parents=True, exist_ok=True)
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)

            print_summary_report(
                benchmark_name="Retrieval Benchmark",
                total_q=len(self._cases),
                wall_time_s=wall_time,
                overall_metric=hybrid_rec5,
                metric_name="Recall@5 (Hybrid)",
                latency_stats=lat_stats,
                retrieval_recalls=retrieval_recalls,
                output_path=out_file,
            )

            return report
        finally:
            self._teardown_db()


if __name__ == "__main__":
    bench = RetrievalBenchmark()
    bench.run()

