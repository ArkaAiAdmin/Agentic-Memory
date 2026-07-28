#!/usr/bin/env python3
"""Real-memory golden evaluation harness for search pipeline SOTA validation.

Measures recall@10, MRR, and latency against the golden set in
real_memory_golden.json.  Targets (from Phase 8 plan):
  - recall@10 ≥ 0.92
  - MRR ≥ 0.85
  - P95 cold latency ≤ 600 ms
  - P95 warm latency ≤ 200 ms

Usage:
  venv/bin/python eval/real_memory_eval.py [--db MEMORY_DB_PATH]
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

# Bootstrap DB path (overridden if --db is passed)
_TEST_DB_DIR = tempfile.mkdtemp(prefix="real_memory_eval_")
_TEST_DB_PATH = Path(_TEST_DB_DIR) / "memory.db"
os.environ.setdefault("MEMORY_DB_PATH", str(_TEST_DB_PATH))


def _load_golden_set() -> dict:
    """Load the golden evaluation set."""
    golden_path = Path(__file__).parent / "real_memory_golden_v2.json"
    with open(golden_path) as f:
        return json.load(f)


def _setup_db(db_path: Path) -> sqlite3.Connection:
    """Create a fresh DB with the full schema and populate with golden memories."""
    from infra.db_migrations import run_schema_setup

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    run_schema_setup(conn)

    # Also ensure FTS tables
    try:
        from fact import ensure_facts_schema
        ensure_facts_schema(conn)
    except Exception:
        pass

    # Ensure KG tables exist for KG-based search
    try:
        from knowledge_graph import ensure_kg_schema
        ensure_kg_schema(conn)
    except Exception:
        pass

    conn.commit()
    return conn


def _insert_memories(conn: sqlite3.Connection, memories: list[dict]) -> None:
    """Insert golden memories into the database."""
    for mem in memories:
        try:
            conn.execute(
                """INSERT OR IGNORE INTO memories
                   (id, content, source_file, tags, category, created_at, updated_at,
                    fitness_score, importance, pinned, access_count)
                   VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'), 0.5, 3, 0, 1)""",
                (
                    mem["note_id"],
                    mem["content"],
                    f"{mem['note_id']}.md",
                    json.dumps(mem.get("tags", [])),
                    mem.get("category", ""),
                ),
            )
        except Exception as e:
            print(f"Warning: failed to insert {mem['note_id']}: {e}")
    conn.commit()


def _insert_fts(conn: sqlite3.Connection, memories: list[dict]) -> None:
    """Insert memories into FTS5 index."""
    for mem in memories:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO memories_fts (rowid, id, content, tags) "
                "VALUES ((SELECT rowid FROM memories WHERE id = ?), ?, ?, ?)",
                (
                    mem["note_id"],
                    mem["note_id"],
                    mem["content"],
                    json.dumps(mem.get("tags", [])),
                ),
            )
        except Exception:
            pass
    conn.commit()


def _backfill_indexes(conn: sqlite3.Connection, db_path: Path) -> None:
    """Generate embeddings, chunks, ColBERT, and SPLADE for all memories."""
    rows = conn.execute(
        "SELECT id, content FROM memories WHERE content IS NOT NULL AND content != ''"
    ).fetchall()
    items: list[tuple[str, str, str, list[str] | None]] = [
        (mid, content, "", []) for mid, content in rows
    ]
    from _fixtures import populate_eval_memory_indexes_batch
    populate_eval_memory_indexes_batch(conn, items, use_llm_facts=False)


def _compute_recall_at_k(retrieved: list[str], expected: list[str], k: int = 10) -> float:
    """Compute recall@k."""
    if not expected:
        return 1.0
    retrieved_set = set(retrieved[:k])
    hits = len(set(expected) & retrieved_set)
    return hits / len(expected)


def _compute_precision_at_k(retrieved: list[str], expected: list[str], k: int = 10) -> float:
    """Compute precision@k."""
    if not retrieved[:k]:
        return 0.0
    retrieved_set = set(retrieved[:k])
    hits = len(set(expected) & retrieved_set)
    return hits / min(k, len(retrieved))


def _compute_mrr(retrieved: list[str], expected: list[str]) -> float:
    """Compute Mean Reciprocal Rank."""
    for i, doc_id in enumerate(retrieved):
        if doc_id in expected:
            return 1.0 / (i + 1)
    return 0.0


def _compute_ndcg_at_k(retrieved: list[str], expected: list[str], k: int = 10) -> float:
    """Compute nDCG@k (normalized discounted cumulative gain)."""
    if not expected:
        return 1.0

    # DCG: sum of 1/log2(i+2) for relevant docs at position i
    dcg = 0.0
    for i, doc_id in enumerate(retrieved[:k]):
        if doc_id in expected:
            dcg += 1.0 / (i + 2)  # log2(i+1) + 1, but i starts at 0

    # Ideal DCG: all relevant docs at the top
    ideal_dcg = 0.0
    n_relevant = min(len(expected), k)
    for i in range(n_relevant):
        ideal_dcg += 1.0 / (i + 2)

    if ideal_dcg == 0:
        return 0.0
    return dcg / ideal_dcg


def _search_single(
    db_path: str, query: str, limit: int = 10, as_of: float | None = None
) -> tuple[list[str], float]:
    """Search for a query and return (result_ids, latency_ms).

    Uses a per-query timeout to prevent hangs in the 14-phase pipeline.
    """
    from concurrent.futures import TimeoutError as FutureTimeout
    from search.orchestrator import search_memories

    t0 = time.time()
    try:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                search_memories,
                query=query,
                db_path=Path(db_path),
                limit=limit,
                hybrid=True,
                rerank=True,
                as_of=as_of,
            )
            result = future.result(timeout=120)
        latency_ms = (time.time() - t0) * 1000
        if isinstance(result, dict):
            ids = [r.get("id", "") for r in result.get("results", [])]
        else:
            ids = []
        return ids, latency_ms
    except FutureTimeout:
        latency_ms = (time.time() - t0) * 1000
        print(f"  ⚠ Search TIMEOUT (120s) for '{query}'", flush=True)
        return [], latency_ms
    except Exception as e:
        latency_ms = (time.time() - t0) * 1000
        print(f"  Search failed for '{query}': {e}")
        return [], latency_ms


def run_evaluation(db_path: Path | None = None, verbose: bool = True, skip_backfill: bool = False) -> dict:
    """Run the full golden evaluation.

    Returns a dict with metrics and pass/fail status.
    """
    # Clear reranker caches to prevent cross-run contamination
    try:
        import search.rerankers as _rerankers
        if hasattr(_rerankers, "clear_reranker_caches"):
            _rerankers.clear_reranker_caches()
    except Exception:
        pass

    golden = _load_golden_set()
    targets = golden["targets"]
    memories = golden["memories"]
    test_cases = golden["test_cases"]

    # Setup database
    if db_path is None:
        db_path = _TEST_DB_PATH
    os.environ["MEMORY_DB_PATH"] = str(db_path)
    conn = _setup_db(db_path)
    _insert_memories(conn, memories)
    _insert_fts(conn, memories)
    if not skip_backfill:
        _backfill_indexes(conn, db_path)
    else:
        print("  Skipping backfill (using pre-built DB)")

    # Warm up: pre-load all models before timed queries
    if verbose:
        print("Warming up search pipeline...")
    # Pre-load cross-encoder into module-level cache
    try:
        import search.rerankers as _rerankers
        from sentence_transformers import CrossEncoder
        _rerankers._CE_CHUNK_MODEL = CrossEncoder(
            "cross-encoder/ms-marco-MiniLM-L-6-v2", max_length=512
        )
        if verbose:
            print("  Cross-encoder loaded")
    except Exception as e:
        print(f"  ⚠ Cross-encoder warmup FAILED (reranker disabled): {e}", flush=True)
    # Pre-load SPLADE
    try:
        from infra.splade_encoder import encode_sparse
        encode_sparse("warmup")
        if verbose:
            print("  SPLADE loaded")
    except Exception as e:
        print(f"  ⚠ SPLADE warmup FAILED (sparse index disabled): {e}", flush=True)
    # Run warm-up queries to prime FTS cache
    warmup_queries = [
        "docker container basics", "python testing", "kubernetes deployment",
        "redis caching", "postgresql transaction", "terraform infrastructure",
    ]
    for i, wq in enumerate(warmup_queries):
        if i > 0 and i % 2 == 0 and verbose:
            print(f"  Warmup progress: {i}/{len(warmup_queries)}", flush=True)
        _search_single(str(db_path), wq, limit=5)

    # Run test cases
    results = {
        "recall_at_5": [],
        "recall_at_10": [],
        "recall_at_25": [],
        "recall_at_50": [],
        "precision_at_5": [],
        "precision_at_10": [],
        "mrr": [],
        "mrr_at_5": [],
        "ndcg_at_10": [],
        "hit_rate": [],  # 1 if any expected found, 0 otherwise
        "cold_latencies": [],
        "warm_latencies": [],
        "pass_per_query": [],
        "categories": {},
    }

    for i, tc in enumerate(test_cases):
        query = tc["query"]
        expected = tc["expected"]

        # Temporal query: parse as_of from expected session IDs
        as_of = None
        tc_cat = tc.get("category", "")
        if tc_cat == "temporal":
            import calendar, re, time as _time
            for eid in tc.get("expected", []):
                m = re.search(r'(\d{4})-(\d{2})-(\d{2})', eid)
                if m:
                    as_of = calendar.timegm(_time.strptime(m.group(0), "%Y-%m-%d")) + 86400
                    break
            if as_of is None:
                m = re.search(r'(?:July|2026).*?(\d{1,2})', query)
                if m:
                    as_of = int(m.group(1))
                    # Interpret as day-of-month within the most recent July/2026
                    # for temporal scoring purposes

        # Cold latency (first call after warmup)
        retrieved, latency = _search_single(str(db_path), query, limit=50, as_of=as_of)
        if i == 0:
            results["cold_latencies"].append(latency)
        else:
            results["warm_latencies"].append(latency)

        recall_5 = _compute_recall_at_k(retrieved, expected, k=5)
        recall_10 = _compute_recall_at_k(retrieved, expected, k=10)
        recall_25 = _compute_recall_at_k(retrieved, expected, k=25)
        recall_50 = _compute_recall_at_k(retrieved, expected, k=50)
        precision_5 = _compute_precision_at_k(retrieved, expected, k=5)
        precision_10 = _compute_precision_at_k(retrieved, expected, k=10)
        mrr = _compute_mrr(retrieved, expected)
        mrr_5 = _compute_mrr(retrieved[:5], expected) if len(retrieved) >= 5 else mrr
        ndcg = _compute_ndcg_at_k(retrieved, expected, k=10)
        hit = 1.0 if any(r in expected for r in retrieved) else 0.0

        results["recall_at_5"].append(recall_5)
        results["recall_at_10"].append(recall_10)
        results["recall_at_25"].append(recall_25)
        results["recall_at_50"].append(recall_50)
        results["precision_at_5"].append(precision_5)
        results["precision_at_10"].append(precision_10)
        results["mrr"].append(mrr)
        results["mrr_at_5"].append(mrr_5)
        results["ndcg_at_10"].append(ndcg)
        results["hit_rate"].append(hit)

        # Pass if recall@10 meets per-query threshold
        min_recall = tc.get("min_recall_at_10", 0.8)
        passed = recall_10 >= min_recall
        results["pass_per_query"].append(passed)

        if verbose and not passed:
            print(f"  FAIL: '{query}' — recall@10={recall_10:.2f}, expected={expected}, got={retrieved[:5]}")

        tc_cat = tc.get("category", "unknown")
        cat_key = f"recall_at_10_{tc_cat}"
        if cat_key not in results:
            results[cat_key] = []
        results[cat_key].append(recall_10)

    # Compute aggregate metrics
    avg_recall_5 = sum(results["recall_at_5"]) / len(results["recall_at_5"])
    avg_recall_10 = sum(results["recall_at_10"]) / len(results["recall_at_10"])
    avg_recall_25 = sum(results["recall_at_25"]) / len(results["recall_at_25"])
    avg_recall_50 = sum(results["recall_at_50"]) / len(results["recall_at_50"])
    avg_precision_5 = sum(results["precision_at_5"]) / len(results["precision_at_5"])
    avg_precision_10 = sum(results["precision_at_10"]) / len(results["precision_at_10"])
    avg_mrr = sum(results["mrr"]) / len(results["mrr"])
    avg_mrr_5 = sum(results["mrr_at_5"]) / len(results["mrr_at_5"])
    avg_ndcg = sum(results["ndcg_at_10"]) / len(results["ndcg_at_10"])
    avg_hit_rate = sum(results["hit_rate"]) / len(results["hit_rate"])

    cold_latencies = sorted(results["cold_latencies"])
    warm_latencies = sorted(results["warm_latencies"])

    p95_cold_idx = int(len(cold_latencies) * 0.95)
    p95_warm_idx = int(len(warm_latencies) * 0.95)
    p95_cold = cold_latencies[min(p95_cold_idx, len(cold_latencies) - 1)]
    p95_warm = warm_latencies[min(p95_warm_idx, len(warm_latencies) - 1)]

    passed_queries = sum(results["pass_per_query"])
    total_queries = len(results["pass_per_query"])

    # Check targets
    checks = {
        "recall_at_5": avg_recall_5 >= targets.get("recall_at_5", 0.85),
        "recall_at_10": avg_recall_10 >= targets.get("recall_at_10", 0.92),
        "recall_at_25": avg_recall_25 >= targets.get("recall_at_25", 0.95),
        "recall_at_50": avg_recall_50 >= targets.get("recall_at_50", 0.98),
        "mrr": avg_mrr >= targets.get("mrr", 0.85),
        "ndcg_at_10": avg_ndcg >= targets.get("ndcg_at_10", 0.80),
        "hit_rate": avg_hit_rate >= targets.get("hit_rate", 0.95),
        "p95_cold_latency": p95_cold <= targets.get("p95_cold_latency_ms", 600),
        "p95_warm_latency": p95_warm <= targets.get("p95_warm_latency_ms", 300),
    }
    all_passed = all(checks.values())

    summary = {
        "metrics": {
            "recall_at_5": round(avg_recall_5, 4),
            "recall_at_10": round(avg_recall_10, 4),
            "recall_at_25": round(avg_recall_25, 4),
            "recall_at_50": round(avg_recall_50, 4),
            "precision_at_5": round(avg_precision_5, 4),
            "precision_at_10": round(avg_precision_10, 4),
            "mrr": round(avg_mrr, 4),
            "mrr_at_5": round(avg_mrr_5, 4),
            "ndcg_at_10": round(avg_ndcg, 4),
            "hit_rate": round(avg_hit_rate, 4),
            "p95_cold_latency_ms": round(p95_cold, 1),
            "p95_warm_latency_ms": round(p95_warm, 1),
        },
        "targets": targets,
        "checks": checks,
        "all_passed": all_passed,
        "queries_passed": f"{passed_queries}/{total_queries}",
        "total_queries": total_queries,
    }

    if verbose:
        print("\n" + "=" * 70)
        print("REAL-MEMORY GOLDEN EVAL RESULTS")
        print("=" * 70)
        print(f"  recall@5:       {avg_recall_5:.4f} (target: {targets.get('recall_at_5', 0.85)}) {'PASS' if checks['recall_at_5'] else 'FAIL'}")
        print(f"  recall@10:      {avg_recall_10:.4f} (target: {targets.get('recall_at_10', 0.92)}) {'PASS' if checks['recall_at_10'] else 'FAIL'}")
        print(f"  recall@25:      {avg_recall_25:.4f} (target: {targets.get('recall_at_25', 0.95)}) {'PASS' if checks['recall_at_25'] else 'FAIL'}")
        print(f"  recall@50:      {avg_recall_50:.4f} (target: {targets.get('recall_at_50', 0.98)}) {'PASS' if checks['recall_at_50'] else 'FAIL'}")
        print(f"  precision@5:    {avg_precision_5:.4f}")
        print(f"  precision@10:   {avg_precision_10:.4f}")
        print(f"  MRR:            {avg_mrr:.4f} (target: {targets.get('mrr', 0.85)}) {'PASS' if checks['mrr'] else 'FAIL'}")
        print(f"  MRR@5:          {avg_mrr_5:.4f}")
        print(f"  nDCG@10:        {avg_ndcg:.4f} (target: {targets.get('ndcg_at_10', 0.80)}) {'PASS' if checks['ndcg_at_10'] else 'FAIL'}")
        print(f"  hit_rate:       {avg_hit_rate:.4f} (target: {targets.get('hit_rate', 0.95)}) {'PASS' if checks['hit_rate'] else 'FAIL'}")
        print(f"  P95 cold:       {p95_cold:.1f} ms (target: {targets.get('p95_cold_latency_ms', 600)} ms) {'PASS' if checks['p95_cold_latency'] else 'FAIL'}")
        print(f"  P95 warm:       {p95_warm:.1f} ms (target: {targets.get('p95_warm_latency_ms', 300)} ms) {'PASS' if checks['p95_warm_latency'] else 'FAIL'}")
        print(f"  queries passed: {passed_queries}/{total_queries}")

        cat_results = {}
        for key, vals in results.items():
            if key.startswith("recall_at_10_"):
                cat = key.replace("recall_at_10_", "")
                cat_results[cat] = sum(vals) / len(vals) if vals else 0.0
        if cat_results:
            print("\n  Per-category recall@10:")
            for cat in sorted(cat_results):
                status = "PASS" if cat_results[cat] >= 0.95 else "FAIL"
                print(f"    {cat:20} {cat_results[cat]:.4f}  {status}")

        print(f"  OVERALL:        {'ALL TARGETS MET' if all_passed else 'TARGETS NOT MET'}")
        print("=" * 70)

    conn.close()
    return summary


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Real-memory golden evaluation")
    parser.add_argument("--db", type=str, default=None, help="Database path")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-query output")
    parser.add_argument("--skip-backfill", action="store_true", help="Skip index backfill (use pre-built DB)")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else None
    if db_path is not None:
        os.environ["MEMORY_DB_PATH"] = str(db_path)
    result = run_evaluation(db_path=db_path, verbose=not args.quiet, skip_backfill=args.skip_backfill)

    # Write results to file
    results_path = Path(__file__).parent / "real_memory_eval_results.json"
    with open(results_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nResults saved to {results_path}")

    # Exit code: 0 if all targets met, 1 otherwise
    sys.exit(0 if result["all_passed"] else 1)
