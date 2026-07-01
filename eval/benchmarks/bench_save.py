#!/usr/bin/env python3
"""Benchmark: memory save latency across corpus sizes.

Creates synthetic corpora (100, 1K, 10K notes), measures save latency
(p50/p95/p99/max), and writes results to eval/results/bench-save.json.
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
RESULTS_PATH = REPO_ROOT / "eval" / "results" / "bench-save.json"

sys.path.insert(0, str(REPO_ROOT))

CORPUS_SIZES = [100, 1_000, 10_000]
BODY = "lorem ipsum dolor sit amet consectetur adipiscing elit"


def _build_corpus(size: int) -> list[dict]:
    return [
        {
            "content": f"---\ncategory: lessons\ntitle_slug: bench-{i}\n---\n\n# Note {i}\n\n{BODY}",
            "category": "lessons",
            "title_slug": f"bench-{i}",
            "tags": ["benchmark", "save"],
            "importance": 3,
        }
        for i in range(size)
    ]


def _measure(note: dict, db_path: str) -> float:
    from save_pipeline import save_memory
    t0 = time.time()
    save_memory(
        content=note["content"],
        category=note["category"],
        title_slug=note["title_slug"],
        tags=note["tags"],
        importance=note["importance"],
        safety_wiring=False,
        db_path=db_path,
    )
    return (time.time() - t0) * 1000.0


def run_bench(quick: bool = False) -> dict:
    from infra.memory_common import GLOBAL_MEM_DIR
    import shutil

    results: dict = {}
    sizes = [100] if quick else CORPUS_SIZES

    for size in sizes:
        corpus = _build_corpus(size)
        tmpdir = Path(tempfile.mkdtemp(prefix=f"bench-save-{size}-"))
        db_path = str(tmpdir / "memory.db")
        _bootstrap_db(db_path)

        latencies: list[float] = []
        for note in corpus:
            lat = _measure(note, db_path)
            latencies.append(lat)

        stats = _compute_stats(latencies)
        results[str(size)] = {
            "count": size,
            "latency_ms": stats,
            "total_ms": sum(latencies),
        }
        shutil.rmtree(tmpdir, ignore_errors=True)

    return {
        "benchmark": "save",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": results,
    }


def _bootstrap_db(db_path: str):
    import sqlite3
    from infra.db_migrations import run_schema_setup
    from fact import ensure_facts_schema
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        run_schema_setup(conn)
        ensure_facts_schema(conn)
        conn.commit()
    finally:
        conn.close()


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
    parser = argparse.ArgumentParser(description="Save latency benchmark")
    parser.add_argument("--quick", action="store_true", help="100 notes only")
    args = parser.parse_args()

    data = run_bench(quick=args.quick)
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(data, indent=2))
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
