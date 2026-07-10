#!/usr/bin/env python3
"""Benchmark: search latency across corpus sizes.

Creates synthetic corpora (100, 1K, 10K notes), measures FTS5 and hybrid
search latency (p50/p95/p99/max), and writes results to
eval/results/bench-search.json.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS_PATH = REPO_ROOT / "eval" / "results" / "bench-search.json"

sys.path.insert(0, str(REPO_ROOT))

CORPUS_SIZES = [100, 1_000, 10_000]
QUERIES = [
    "lorem ipsum",
    "consectetur adipiscing",
    "dolor sit amet",
    "benchmark test",
    "memory system performance",
]


def _build_corpus(size: int) -> list[dict]:
    return [
        {
            "content": f"---\ncategory: lessons\ntitle_slug: srch-{i}\n---\n\n# Search Note {i}\n\n"
            f"the quick brown fox jumps over the lazy dog {i} "
            f"lorem ipsum dolor sit amet consectetur adipiscing elit {i} "
            f"benchmark search test memory note {i}",
            "category": "lessons",
            "title_slug": f"srch-{i}",
            "tags": ["benchmark", "search"],
        }
        for i in range(size)
    ]


def _populate_db(db_path: str, corpus: list[dict]):
    from save_pipeline import save_memory
    for note in corpus:
        save_memory(
            content=note["content"],
            category=note["category"],
            title_slug=note["title_slug"],
            tags=note["tags"],
            safety_wiring=False,
            db_path=db_path,
        )


def _measure_search(db_path: str, query: str, mode: str) -> float:
    from pathlib import Path as _Path

    from search.orchestrator import search_memories
    # `mode` is "fts" (BM25-only) or "hybrid" (BM25 + vector fusion).
    # search_memories exposes this via the `hybrid` boolean flag.
    t0 = time.time()
    search_memories(
        db_path=_Path(db_path),
        query=query,
        limit=10,
        include_global=False,
        safety_wiring=False,
        hybrid=(mode == "hybrid"),
    )
    return (time.time() - t0) * 1000.0


def run_bench(quick: bool = False) -> dict:
    import shutil
    import sqlite3
    from infra.db_migrations import run_schema_setup
    from fact import ensure_facts_schema

    results: dict = {}
    sizes = [100] if quick else CORPUS_SIZES

    for size in sizes:
        corpus = _build_corpus(size)
        tmpdir = Path(tempfile.mkdtemp(prefix=f"bench-search-{size}-"))
        db_path = str(tmpdir / "memory.db")

        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            run_schema_setup(conn)
            ensure_facts_schema(conn)
            conn.commit()
        finally:
            conn.close()

        _populate_db(db_path, corpus)

        for mode in ("fts", "hybrid"):
            latencies: list[float] = []
            for q in QUERIES:
                lat = _measure_search(db_path, q, mode)
                latencies.append(lat)
            stats = _compute_stats(latencies)
            results.setdefault(str(size), {})[mode] = stats

        shutil.rmtree(tmpdir, ignore_errors=True)

    return {
        "benchmark": "search",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": results,
    }


def _compute_stats(latencies: list[float]) -> dict:
    if not latencies:
        return {"p50": 0, "p95": 0, "p99": 0, "max": 0, "mean": 0}
    s = sorted(latencies)
    return {
        "p50": s[len(s) // 2],
        "p95": s[int(len(s) * 0.95)],
        "p99": s[int(len(s) * 0.99)],
        "max": s[-1],
        "mean": statistics.mean(latencies),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Search latency benchmark")
    parser.add_argument("--quick", action="store_true", help="100 notes only")
    args = parser.parse_args()

    data = run_bench(quick=args.quick)
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(data, indent=2))
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
