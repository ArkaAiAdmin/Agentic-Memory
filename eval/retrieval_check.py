#!/usr/bin/env python3
"""Retrieval regression check harness for agentic-memory.

Runs a gold JSONL file of queries against one or more memory DBs, computes
retrieval-quality metrics (nDCG@5, MRR, 0-result rate, latency), optionally
compares against a saved baseline, and returns a non-zero exit code if any
metric regressed past tolerance.

Usage:
    python eval/retrieval_check.py --gold eval/gold/v1.jsonl
    python eval/retrieval_check.py --gold eval/gold/v1.jsonl --corpus /path/to/memory.db
    python eval/retrieval_check.py --gold eval/gold/v1.jsonl --baseline eval/results/retrieval-baseline.json
    python eval/retrieval_check.py --gold eval/gold/v1.jsonl --hybrid false
"""

import argparse
import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

INSTALL_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, INSTALL_ROOT)

import memory_mcp  # noqa: E402

EVAL_ROOT = Path(__file__).resolve().parent
RESULTS_DIR = EVAL_ROOT / "results"
DEFAULT_BASELINE = RESULTS_DIR / "retrieval-baseline.json"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def compute_ndcg_at_5(retrieved_ids, gold_ids, gold_relevances):
    """nDCG@5. If gold is empty, returns 1.0 (no expectation)."""
    if not gold_ids:
        return 1.0
    gold_map = {gid: rel for gid, rel in zip(gold_ids, gold_relevances)}
    dcg = 0.0
    for i, rid in enumerate(retrieved_ids[:5]):
        rel = gold_map.get(rid, 0)
        dcg += rel / math.log2(i + 2)  # i=0 -> log2(1)=0 -> 1/1=1; i=1 -> log2(2)=1
    sorted_rels = sorted(gold_relevances, reverse=True)[:5]
    idcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(sorted_rels))
    if idcg == 0.0:
        return 0.0
    return dcg / idcg


def compute_mrr(retrieved_ids, gold_ids, k=5):
    """Mean reciprocal rank at k. Empty gold -> 1.0."""
    if not gold_ids:
        return 1.0
    gold_set = set(gold_ids)
    for i, rid in enumerate(retrieved_ids[:k]):
        if rid in gold_set:
            return 1.0 / (i + 1)
    return 0.0


def percentile(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p / 100.0
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


# ---------------------------------------------------------------------------
# Git SHA
# ---------------------------------------------------------------------------


def get_git_sha():
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        return out or "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------


def run_query(query, corpus, limit, hybrid, db_housekeeping):
    """Execute one query, return (ids, latency_ms, error)."""
    db_path = Path(corpus)
    if not db_path.exists():
        return [], 0.0, "db_not_found"
    if corpus not in db_housekeeping:
        # Open a per-corpus connection for PRAGMA housekeeping.
        # The actual search goes through memory_mcp.search_memories which
        # opens its own connection; we just set a process-wide warm cache.
        try:
            conn = sqlite3.connect(str(db_path), timeout=30.0)
            conn.execute("PRAGMA busy_timeout = 30000;")
            conn.execute("PRAGMA temp_store = MEMORY;")
            conn.execute("PRAGMA cache_size = -64000;")
            db_housekeeping[corpus] = conn
        except Exception:
            db_housekeeping[corpus] = None
    t0 = time.perf_counter()
    try:
        result = memory_mcp.search_memories(
            db_path,
            query,
            limit=limit,
            hybrid=hybrid,
            use_history=False,  # don't pollute bb2 turn buffer
        )
    except Exception as e:
        return [], (time.perf_counter() - t0) * 1000.0, f"exception:{e}"
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    ids = [r.get("id") for r in (result.get("results") or []) if r.get("id")]
    return ids, elapsed_ms, None


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


def load_gold(path):
    entries = []
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError as ex:
                raise SystemExit(f"gold file line {lineno}: bad json: {ex}")
            for required in ("id", "query", "corpus", "gold_ids"):
                if required not in e:
                    raise SystemExit(
                        f"gold file line {lineno}: missing required field '{required}'"
                    )
            e.setdefault("relevance", [3] * len(e["gold_ids"]))
            entries.append(e)
    return entries


def main():
    parser = argparse.ArgumentParser(
        description="Retrieval regression check for agentic-memory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Run all 100 queries against their respective corpora
  python eval/retrieval_check.py --gold eval/gold/v1.jsonl

  # Run only against the lmeval DB (skip the project DBs)
  python eval/retrieval_check.py --gold eval/gold/v1.jsonl --corpus memory/memory.db

  # Compare against the saved baseline
  python eval/retrieval_check.py --gold eval/gold/v1.jsonl --baseline eval/results/retrieval-baseline.json

  # Faster run, BM25 only (no embeddings)
  python eval/retrieval_check.py --gold eval/gold/v1.jsonl --hybrid false
        """,
    )
    parser.add_argument(
        "--gold", required=False, default=None, help="path to gold JSONL file"
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help="path to a previous baseline JSON for comparison",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.05,
        help="max nDCG@5 drop before flagging regression (default 0.05)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="path to write the run results JSON (default: "
        "eval/results/retrieval-run-<timestamp>.json)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="max number of queries to execute (default 100 = all)",
    )
    parser.add_argument(
        "--hybrid",
        default="true",
        help="pass through to search_memories: true|false (default true)",
    )
    parser.add_argument(
        "--corpus",
        default=None,
        help="override the per-query corpus (run all queries against one DB)",
    )
    parser.add_argument(
        "--self-test", action="store_true", help="run the built-in self-test and exit"
    )
    args = parser.parse_args()

    if getattr(args, "self_test", False):
        ok = _self_test()
        sys.exit(0 if ok else 2)

    if not args.gold:
        print("ERROR: --gold is required (or pass --self-test)", file=sys.stderr)
        sys.exit(2)

    if not os.path.exists(args.gold):
        print(f"ERROR: gold file not found: {args.gold}", file=sys.stderr)
        sys.exit(2)

    hybrid = str(args.hybrid).lower() in ("1", "true", "yes", "y")
    entries = load_gold(args.gold)
    if args.corpus:
        for e in entries:
            e["corpus"] = args.corpus
    entries = entries[: args.limit]

    if not entries:
        print("ERROR: no queries to execute", file=sys.stderr)
        sys.exit(2)

    # Group by corpus for reporting
    by_corpus_groups = {}
    for e in entries:
        by_corpus_groups.setdefault(e["corpus"], []).append(e)

    # ---- execute ----
    db_housekeeping = {}  # per-corpus sqlite connection for PRAGMAs
    latencies = []
    ndcgs = []
    mrrs = []
    zero_count = 0
    per_query_records = []
    t_start = time.perf_counter()

    total = len(entries)
    progress_every = max(1, total // 20)  # ~20 progress updates

    for idx, e in enumerate(entries, 1):
        corpus = e["corpus"]
        gold_ids = list(e.get("gold_ids") or [])
        rel = list(e.get("relevance") or [3] * len(gold_ids))
        if len(rel) < len(gold_ids):
            rel = rel + [3] * (len(gold_ids) - len(rel))

        ids, latency_ms, err = run_query(
            e["query"],
            corpus,
            limit=5,
            hybrid=hybrid,
            db_housekeeping=db_housekeeping,
        )
        ndcg = compute_ndcg_at_5(ids, gold_ids, rel)
        mrr = compute_mrr(ids, gold_ids, k=5)
        if len(ids) == 0:
            zero_count += 1

        latencies.append(latency_ms)
        ndcgs.append(ndcg)
        mrrs.append(mrr)
        per_query_records.append(
            {
                "id": e["id"],
                "query": e["query"],
                "corpus": corpus,
                "expected": gold_ids,
                "got_top_5": ids,
                "ndcg": ndcg,
                "mrr": mrr,
                "latency_ms": latency_ms,
                "error": err,
            }
        )

        if idx % progress_every == 0 or idx == total:
            pct = 100.0 * idx / total
            sys.stdout.write(f"\r  progress: {idx}/{total} ({pct:.0f}%)")
            sys.stdout.flush()
    sys.stdout.write("\n")

    wall_time_ms = (time.perf_counter() - t_start) * 1000.0

    # Close housekeeping connections
    for c in db_housekeeping.values():
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    n = len(per_query_records)
    mean_ndcg = sum(ndcgs) / n
    mean_mrr = sum(mrrs) / n
    zero_rate = zero_count / n if n else 0.0
    p50 = percentile(latencies, 50)
    p95 = percentile(latencies, 95)

    # ---- per-corpus breakdown ----
    by_corpus = {}
    for corpus, group in by_corpus_groups.items():
        {r["id"] for r in per_query_records if r["corpus"] == corpus}
        g_ndcg = [r["ndcg"] for r in per_query_records if r["corpus"] == corpus]
        g_mrr = [r["mrr"] for r in per_query_records if r["corpus"] == corpus]
        g_zero = sum(
            1
            for r in per_query_records
            if r["corpus"] == corpus and len(r["got_top_5"]) == 0
        )
        by_corpus[corpus] = {
            "ndcg_at_5": sum(g_ndcg) / len(g_ndcg) if g_ndcg else 0.0,
            "mrr": sum(g_mrr) / len(g_mrr) if g_mrr else 0.0,
            "zero_result_rate": g_zero / len(g_ndcg) if g_ndcg else 0.0,
            "count": len(g_ndcg),
        }

    # ---- per-query-pattern breakdown ----
    with_q = [r for r in per_query_records if '"' in r["query"]]
    no_q = [r for r in per_query_records if '"' not in r["query"]]
    by_query_pattern = {}
    if with_q:
        by_query_pattern["with_quotes"] = {
            "ndcg_at_5": sum(r["ndcg"] for r in with_q) / len(with_q),
            "mrr": sum(r["mrr"] for r in with_q) / len(with_q),
            "count": len(with_q),
        }
    if no_q:
        by_query_pattern["without_quotes"] = {
            "ndcg_at_5": sum(r["ndcg"] for r in no_q) / len(no_q),
            "mrr": sum(r["mrr"] for r in no_q) / len(no_q),
            "count": len(no_q),
        }

    # ---- worst 5 / best 5 ----
    def _reason(r):
        if not r["expected"]:
            return "no_gold_specified"
        if not r["got_top_5"]:
            return "zero_results"
        if not any(g in r["got_top_5"] for g in r["expected"]):
            return "no_gold_in_top5"
        return ""

    sorted_by_ndcg = sorted(per_query_records, key=lambda r: (r["ndcg"], r["mrr"]))
    worst_5 = []
    for r in sorted_by_ndcg[:5]:
        worst_5.append(
            {
                "id": r["id"],
                "query": r["query"],
                "expected": r["expected"],
                "got_top_5": r["got_top_5"],
                "ndcg": r["ndcg"],
                "mrr": r["mrr"],
                "reason": _reason(r),
            }
        )
    best_5 = []
    for r in sorted(per_query_records, key=lambda r: (-r["ndcg"], -r["mrr"]))[:5]:
        best_5.append(
            {
                "id": r["id"],
                "query": r["query"],
                "ndcg": r["ndcg"],
                "mrr": r["mrr"],
            }
        )

    result = {
        "schema_version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": get_git_sha(),
        "gold_file": str(args.gold),
        "limit": args.limit,
        "hybrid": hybrid,
        "total_queries": n,
        "queries_executed": n,
        "queries_skipped": 0,
        "wall_time_ms": round(wall_time_ms, 1),
        "ndcg_at_5": round(mean_ndcg, 6),
        "mrr": round(mean_mrr, 6),
        "zero_result_rate": round(zero_rate, 6),
        "latency_p50_ms": round(p50, 3),
        "latency_p95_ms": round(p95, 3),
        "by_corpus": by_corpus,
        "by_query_pattern": by_query_pattern,
        "worst_5_queries": worst_5,
        "best_5_queries": best_5,
    }

    # ---- write output ----
    if args.output:
        out_path = Path(args.output)
    else:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = RESULTS_DIR / f"retrieval-run-{ts}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    # ---- print summary ----
    print("=" * 70)
    print("  retrieval check complete")
    print(f"  nDCG@5         : {mean_ndcg:.4f}")
    print(f"  MRR            : {mean_mrr:.4f}")
    print(f"  0-result rate  : {zero_rate:.4f}  ({zero_count}/{n})")
    print(f"  latency p50/p95: {p50:.2f}ms / {p95:.2f}ms")
    print(f"  wall time      : {wall_time_ms / 1000.0:.2f}s")
    print(f"  output         : {out_path}")
    for corpus, m in by_corpus.items():
        print(
            f"    [{Path(corpus).parent.name}/{Path(corpus).name}] "
            f"n={m['count']:3d}  ndcg={m['ndcg_at_5']:.4f}  "
            f"mrr={m['mrr']:.4f}  zero={m['zero_result_rate']:.4f}"
        )
    print("=" * 70)

    # ---- baseline comparison ----
    baseline_path = (
        Path(args.baseline)
        if args.baseline
        else (DEFAULT_BASELINE if DEFAULT_BASELINE.exists() else None)
    )
    if baseline_path and baseline_path.exists():
        try:
            with open(baseline_path) as f:
                base = json.load(f)
        except Exception as ex:
            print(
                f"WARN: could not read baseline {baseline_path}: {ex}", file=sys.stderr
            )
            base = None
        if base:
            delta_ndcg = result["ndcg_at_5"] - base.get("ndcg_at_5", 0.0)
            delta_mrr = result["mrr"] - base.get("mrr", 0.0)
            delta_zero = result["zero_result_rate"] - base.get("zero_result_rate", 0.0)
            ndcg_threshold = -abs(args.tolerance)
            mrr_threshold = -0.05
            zero_threshold = 0.10

            print("-" * 70)
            print(f"  vs baseline ({baseline_path})")
            print(
                f"  delta nDCG@5   : {delta_ndcg:+.4f}   (threshold {ndcg_threshold:+.2f})"
            )
            print(
                f"  delta MRR      : {delta_mrr:+.4f}   (threshold {mrr_threshold:+.2f})"
            )
            print(
                f"  delta 0-result : {delta_zero:+.4f}   (threshold {zero_threshold:+.2f})"
            )
            # per-corpus deltas
            base_by_corpus = base.get("by_corpus", {}) or {}
            for corpus, m in by_corpus.items():
                if corpus in base_by_corpus:
                    bc = base_by_corpus[corpus]
                    dn = m["ndcg_at_5"] - bc.get("ndcg_at_5", 0.0)
                    dm = m["mrr"] - bc.get("mrr", 0.0)
                    dz = m["zero_result_rate"] - bc.get("zero_result_rate", 0.0)
                    print(
                        f"    [{Path(corpus).parent.name}] "
                        f"delta_ndcg={dn:+.4f}  delta_mrr={dm:+.4f}  delta_zero={dz:+.4f}"
                    )
            print("-" * 70)

            regression = (
                delta_ndcg < ndcg_threshold
                or delta_mrr < mrr_threshold
                or delta_zero > zero_threshold
            )
            if regression:
                print("REGRESSION DETECTED — exit 1")
                sys.exit(1)
            print("OK — within tolerance")
    else:
        if args.baseline:
            print(
                f"NOTE: --baseline {args.baseline} not found; skipping comparison",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _self_test():
    """Build a tiny in-memory DB + gold file, run the harness, verify metrics."""
    tmp = Path(tempfile.mkdtemp(prefix="retrieval_selftest_"))
    db_path = tmp / "selftest.db"
    gold_path = tmp / "selftest.jsonl"

    # 1. Build a tiny DB with 5 known notes.
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            source_file TEXT,
            tags TEXT DEFAULT '[]',
            created_at TEXT,
            updated_at TEXT,
            observed_at TEXT,
            pinned INTEGER DEFAULT 0,
            importance INTEGER DEFAULT 3,
            decay TEXT DEFAULT 'none',
            score REAL DEFAULT 1.0,
            supersedes TEXT,
            repo_id TEXT,
            access_count INTEGER DEFAULT 1,
            success_score REAL DEFAULT 0.0,
            fitness_score REAL DEFAULT 1.0,
            conflict_policy TEXT DEFAULT 'supersede',
            version_vector TEXT DEFAULT '{}',
            logical_clock INTEGER DEFAULT 0,
            consolidation_state TEXT DEFAULT 'working',
            valid_from TEXT,
            valid_to TEXT,
            superseded_by TEXT,
            last_accessed TEXT
        );
        CREATE VIRTUAL TABLE memories_fts USING fts5(
            content, tags,
            content='memories', content_rowid='rowid',
            tokenize='unicode61'
        );
        CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, content, tags)
            VALUES (new.rowid, new.content, new.tags);
        END;
        CREATE TABLE backlinks (
            source_id TEXT,
            target_id TEXT,
            PRIMARY KEY (source_id, target_id)
        );
        CREATE INDEX idx_backlinks_target_id ON backlinks(target_id);
    """)
    notes = [
        ("note1", "The capital of France is Paris", "lessons/fr.md"),
        ("note2", "Python is a programming language", "lessons/py.md"),
        ("note3", "The Eiffel Tower is in Paris", "lessons/eiffel.md"),
        ("note4", "JavaScript runs in browsers", "lessons/js.md"),
        ("note5", "Crocodiles are large reptiles", "lessons/animals.md"),
    ]
    now = "2025-01-01T00:00:00"
    for nid, content, src in notes:
        conn.execute(
            "INSERT INTO memories (id, content, source_file, created_at, updated_at, "
            "observed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (nid, content, src, now, now, now),
        )
    conn.commit()
    conn.close()

    # 2. Build a gold file with 3 perfect queries.
    gold = [
        {
            "id": "t1",
            "query": "Paris capital France",
            "corpus": str(db_path),
            "gold_ids": ["note1"],
            "relevance": [3],
        },
        {
            "id": "t2",
            "query": "Python programming language",
            "corpus": str(db_path),
            "gold_ids": ["note2"],
            "relevance": [3],
        },
        {
            "id": "t3",
            "query": "Eiffel Tower Paris",
            "corpus": str(db_path),
            "gold_ids": ["note3"],
            "relevance": [3],
        },
    ]
    with open(gold_path, "w") as f:
        for e in gold:
            f.write(json.dumps(e) + "\n")

    # 3. Run the harness in-process.
    out_path = tmp / "results.json"
    saved_argv = sys.argv
    try:
        sys.argv = [
            "retrieval_check.py",
            "--gold",
            str(gold_path),
            "--output",
            str(out_path),
            "--hybrid",
            "false",
            "--limit",
            "100",
        ]
        try:
            main()
        except SystemExit as ex:
            if ex.code not in (0, None):
                print(f"self-test FAILED: harness exited {ex.code}")
                return False
    finally:
        sys.argv = saved_argv

    # 4. Verify metrics.
    if not out_path.exists():
        print("self-test FAILED: output file not written")
        return False
    with open(out_path) as f:
        r = json.load(f)

    if r.get("total_queries") != 3:
        print(f"self-test FAILED: total_queries={r.get('total_queries')}, expected 3")
        return False
    if abs(r["ndcg_at_5"] - 1.0) > 1e-6:
        print(f"self-test FAILED: ndcg_at_5={r['ndcg_at_5']}, expected 1.0")
        return False
    if abs(r["mrr"] - 1.0) > 1e-6:
        print(f"self-test FAILED: mrr={r['mrr']}, expected 1.0")
        return False
    if r["zero_result_rate"] != 0.0:
        print(
            f"self-test FAILED: zero_result_rate={r['zero_result_rate']}, expected 0.0"
        )
        return False
    # Verify worst_5 / best_5 are non-empty and correctly structured
    if not r.get("best_5_queries"):
        print("self-test FAILED: best_5_queries empty")
        return False
    for q in r["best_5_queries"]:
        if q["ndcg"] != 1.0:
            print(f"self-test FAILED: best_5 query {q['id']} ndcg != 1.0")
            return False
    print("self-test PASSED")
    return True


if __name__ == "__main__":
    main()
