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

# Bootstrap DB path
_TEST_DB_DIR = tempfile.mkdtemp(prefix="real_memory_eval_")
_TEST_DB_PATH = Path(_TEST_DB_DIR) / "memory.db"
os.environ["MEMORY_DB_PATH"] = str(_TEST_DB_PATH)


def _load_golden_set() -> dict:
    """Load the golden evaluation set."""
    golden_path = Path(__file__).parent / "real_memory_golden.json"
    with open(golden_path) as f:
        return json.load(f)


def _setup_db(db_path: Path) -> sqlite3.Connection:
    """Create a fresh DB with the full schema and populate with golden memories."""
    from infra.db_migrations import run_schema_setup

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    run_schema_setup(conn)

    # Also ensure FTS tables
    try:
        from fact import ensure_facts_schema
        ensure_facts_schema(conn)
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


def _compute_recall_at_k(retrieved: list[str], expected: list[str], k: int = 10) -> float:
    """Compute recall@k."""
    if not expected:
        return 1.0
    retrieved_set = set(retrieved[:k])
    hits = len(set(expected) & retrieved_set)
    return hits / len(expected)


def _compute_mrr(retrieved: list[str], expected: list[str]) -> float:
    """Compute Mean Reciprocal Rank."""
    for i, doc_id in enumerate(retrieved):
        if doc_id in expected:
            return 1.0 / (i + 1)
    return 0.0


def _search_single(
    conn: sqlite3.Connection, query: str, limit: int = 10
) -> tuple[list[str], float]:
    """Search for a query and return (result_ids, latency_ms)."""
    from search.orchestrator import search_memories

    t0 = time.time()
    try:
        result = search_memories(
            query=query,
            db_path=conn,
            limit=limit,
            hybrid=False,  # FTS only for consistent evaluation
        )
        latency_ms = (time.time() - t0) * 1000
        if isinstance(result, dict):
            ids = [r.get("id", "") for r in result.get("results", [])]
        else:
            ids = []
        return ids, latency_ms
    except Exception as e:
        latency_ms = (time.time() - t0) * 1000
        print(f"  Search failed for '{query}': {e}")
        return [], latency_ms


def run_evaluation(db_path: Path | None = None, verbose: bool = True) -> dict:
    """Run the full golden evaluation.

    Returns a dict with metrics and pass/fail status.
    """
    golden = _load_golden_set()
    targets = golden["targets"]
    memories = golden["memories"]
    test_cases = golden["test_cases"]

    # Setup database
    if db_path is None:
        db_path = _TEST_DB_PATH
    conn = _setup_db(db_path)
    _insert_memories(conn, memories)
    _insert_fts(conn, memories)

    # Warm up (first query loads models)
    if verbose:
        print("Warming up search pipeline...")
    _search_single(conn, "warmup query", limit=5)

    # Run test cases
    results = {
        "recall_at_10": [],
        "mrr": [],
        "cold_latencies": [],
        "warm_latencies": [],
        "pass_per_query": [],
    }

    for i, tc in enumerate(test_cases):
        query = tc["query"]
        expected = tc["expected"]

        # Cold latency (first call after warmup)
        if i == 0:
            retrieved, latency = _search_single(conn, query, limit=10)
            results["cold_latencies"].append(latency)
        else:
            retrieved, latency = _search_single(conn, query, limit=10)

        results["warm_latencies"].append(latency)

        recall = _compute_recall_at_k(retrieved, expected, k=10)
        mrr = _compute_mrr(retrieved, expected)

        results["recall_at_10"].append(recall)
        results["mrr"].append(mrr)

        passed = recall >= tc.get("min_recall_at_10", 1.0)
        results["pass_per_query"].append(passed)

        if verbose and not passed:
            print(f"  FAIL: '{query}' — recall={recall:.2f}, expected={expected}, got={retrieved[:5]}")

    # Compute aggregate metrics
    avg_recall = sum(results["recall_at_10"]) / len(results["recall_at_10"])
    avg_mrr = sum(results["mrr"]) / len(results["mrr"])

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
        "recall_at_10": avg_recall >= targets["recall_at_10"],
        "mrr": avg_mrr >= targets["mrr"],
        "p95_cold_latency": p95_cold <= targets["p95_cold_latency_ms"],
        "p95_warm_latency": p95_warm <= targets["p95_warm_latency_ms"],
    }
    all_passed = all(checks.values())

    summary = {
        "metrics": {
            "recall_at_10": round(avg_recall, 4),
            "mrr": round(avg_mrr, 4),
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
        print("\n" + "=" * 60)
        print("REAL-MEMORY GOLDEN EVAL RESULTS")
        print("=" * 60)
        print(f"  recall@10:      {avg_recall:.4f} (target: {targets['recall_at_10']}) {'PASS' if checks['recall_at_10'] else 'FAIL'}")
        print(f"  MRR:            {avg_mrr:.4f} (target: {targets['mrr']}) {'PASS' if checks['mrr'] else 'FAIL'}")
        print(f"  P95 cold:       {p95_cold:.1f} ms (target: {targets['p95_cold_latency_ms']} ms) {'PASS' if checks['p95_cold_latency'] else 'FAIL'}")
        print(f"  P95 warm:       {p95_warm:.1f} ms (target: {targets['p95_warm_latency_ms']} ms) {'PASS' if checks['p95_warm_latency'] else 'FAIL'}")
        print(f"  queries passed: {passed_queries}/{total_queries}")
        print(f"  OVERALL:        {'ALL TARGETS MET' if all_passed else 'TARGETS NOT MET'}")
        print("=" * 60)

    conn.close()
    return summary


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Real-memory golden evaluation")
    parser.add_argument("--db", type=str, default=None, help="Database path")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-query output")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else None
    result = run_evaluation(db_path=db_path, verbose=not args.quiet)

    # Write results to file
    results_path = Path(__file__).parent / "real_memory_eval_results.json"
    with open(results_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nResults saved to {results_path}")

    # Exit code: 0 if all targets met, 1 otherwise
    sys.exit(0 if result["all_passed"] else 1)
