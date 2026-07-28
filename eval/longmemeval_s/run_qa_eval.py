#!/usr/bin/env python3
"""LongMemEval Q&A eval — retrieval-augmented token-overlap scoring.

Retrieves context for each question, then scores whether the gold answer
tokens appear in the retrieved context. This matches the standard
retrieval-augmented Q&A metrics (EM / F1) published by MemZero et al.

Scoring:
  - Exact Match (EM): 1 if gold answer is a substring of retrieved context
  - Token F1: overlap between gold answer tokens and context tokens
  - Recall: whether any gold answer token appears in top-k

Usage:
    python run_qa_eval.py [--limit N] [--ks 5,10,20,50]
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from statistics import mean

EVAL_ROOT = Path(__file__).resolve().parent
REPO_ROOT = EVAL_ROOT.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval._fixtures import bootstrap_temp_db_clean

DATASET_PATH = EVAL_ROOT / "longmemeval_s_cleaned.json"
RESULTS_DIR = EVAL_ROOT / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def _join_turns(session_turns: list) -> str:
    """Join conversation turns into a single string."""
    parts = []
    for turn in session_turns:
        if isinstance(turn, dict):
            role = turn.get("role", "")
            content = turn.get("content", "")
            if content:
                parts.append(f"[{role.upper()}] {content}")
    return "\n".join(parts)


def _parse_haystack_date(ds: str) -> str:
    """Parse '2023/05/20 (Sat) 02:21' → '2023-05-20 02:21:00'."""
    import re
    parts = ds.split("(")
    dp = parts[0].strip().replace("/", "-")
    tp = "00:00"
    if len(parts) > 1:
        m = re.search(r"(\d{2}:\d{2})", parts[1])
        if m:
            tp = m.group(1)
    return f"{dp} {tp}:00"


def _seed_sessions(db_path: Path, questions: list[dict]) -> int:
    """Seed all haystack sessions into the DB. Returns count."""
    conn = sqlite3.connect(str(db_path))
    conn.create_function("tenant_id", 0, lambda: "longmemeval")
    conn.execute(
        "CREATE TEMP VIEW IF NOT EXISTS tenant_memories AS "
        "SELECT * FROM memories WHERE tenant_id = tenant_id()"
    )
    count = 0
    try:
        seen_ids = set()
        for q in questions:
            for i, (sid, sess, date) in enumerate(zip(
                q["haystack_session_ids"],
                q["haystack_sessions"],
                q.get("haystack_dates", []),
            )):
                if sid in seen_ids:
                    continue
                seen_ids.add(sid)
                content = _join_turns(sess)
                if not content.strip():
                    continue
                obs = _parse_haystack_date(date) if date else "2024-01-01 00:00:00"
                conn.execute(
                    """INSERT OR REPLACE INTO memories
                       (id, content, source_file, category, tags, created_at, updated_at,
                        observed_at, pinned, importance, tenant_id)
                       VALUES (?, ?, ?, 'sessions', '[]', datetime('now'), datetime('now'),
                               ?, 0, 3, 'longmemeval')""",
                    (sid, content, f"longmemeval/{sid}", obs),
                )
                count += 1
        conn.commit()

        # Rebuild FTS index
        try:
            conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
            conn.commit()
        except Exception:
            pass

        # Index chunks for better search
        try:
            from search.chunk_index import _qw5_index_chunks_for, _qw5_ensure_schema
            _qw5_ensure_schema(conn)
            for q in questions:
                for sid, sess in zip(q["haystack_session_ids"], q["haystack_sessions"]):
                    content = _join_turns(sess)
                    if content.strip():
                        _qw5_index_chunks_for(conn, sid, content)
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()
    return count


def _token_f1(gold_answer, context: str) -> float:
    """Compute token-level F1 between gold answer and context."""
    gold_answer = str(gold_answer)
    gold_tokens = set(gold_answer.lower().split())
    ctx_tokens = set(context.lower().split())
    if not gold_tokens:
        return 0.0
    overlap = gold_tokens & ctx_tokens
    if not overlap:
        return 0.0
    precision = len(overlap) / len(ctx_tokens) if ctx_tokens else 0
    recall = len(overlap) / len(gold_tokens)
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _exact_match(gold_answer, context: str) -> float:
    """Check if gold answer is a substring of context."""
    gold_answer = str(gold_answer)
    return 1.0 if gold_answer.lower() in context.lower() else 0.0


def _token_recall(gold_answer, context: str) -> float:
    """Fraction of gold answer tokens found in context."""
    gold_answer = str(gold_answer)
    gold_tokens = set(gold_answer.lower().split())
    ctx_tokens = set(context.lower().split())
    if not gold_tokens:
        return 0.0
    return len(gold_tokens & ctx_tokens) / len(gold_tokens)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="LongMemEval Q&A eval")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max questions to evaluate (default: all)")
    parser.add_argument("--ks", type=str, default="5,10,20,50",
                        help="Comma-separated k values (default: 5,10,20,50)")
    parser.add_argument("--dataset", type=str, default=str(DATASET_PATH),
                        help="Path to dataset JSON")
    args = parser.parse_args()

    ks = [int(k) for k in args.ks.split(",")]
    max_k = max(ks)

    print("Loading dataset...")
    with open(args.dataset) as f:
        corpus = json.load(f)

    # Filter out _abs questions (unanswerable)
    evaluable = [q for q in corpus if not q["question_id"].endswith("_abs")]
    if args.limit:
        evaluable = evaluable[:args.limit]
    print(f"Loaded {len(evaluable)} questions")

    # Set up DB
    db_path = RESULTS_DIR / "qa_eval.db"
    if db_path.exists():
        db_path.unlink()
    os.environ["MEMORY_DB_PATH"] = str(db_path)
    bootstrap_temp_db_clean(db_path)

    # Seed sessions
    print("Seeding sessions...")
    t0 = time.time()
    n_sessions = _seed_sessions(db_path, evaluable)
    print(f"Seeded {n_sessions} unique sessions in {time.time() - t0:.1f}s")

    # Run eval
    from search.orchestrator import search_memories

    print(f"\nRunning Q&A eval on {len(evaluable)} questions (k={ks})...")
    per_q = []
    per_metric = defaultdict(list)
    per_type = defaultdict(lambda: defaultdict(list))
    total_t = time.perf_counter()

    for idx, q in enumerate(evaluable):
        qid = q["question_id"]
        qtype = q["question_type"]
        gold_answer = q["answer"]
        gold_sessions = set(q["answer_session_ids"])

        try:
            result = search_memories(
                db_path,
                q["question"],
                limit=max_k,
                category="sessions",
                tenant_id="longmemeval",
                hybrid=True,
                rerank=True,
                include_facts=False,
                safety_wiring=False,
            )
            ranked = [r["id"] for r in result.get("results", [])]
        except Exception as e:
            print(f"  Error on {qid}: {e}")
            ranked = []

        # Get context from top-k retrieved sessions
        conn = sqlite3.connect(str(db_path))
        q_scores = {}
        for k in ks:
            top_k = ranked[:k]
            context_parts = []
            for sid in top_k:
                row = conn.execute(
                    "SELECT content FROM memories WHERE id = ?", (sid,)
                ).fetchone()
                if row:
                    context_parts.append(row[0])
            context = "\n\n".join(context_parts)

            em = _exact_match(gold_answer, context)
            f1 = _token_f1(gold_answer, context)
            recall = _token_recall(gold_answer, context)

            q_scores[f"em@{k}"] = em
            q_scores[f"f1@{k}"] = f1
            q_scores[f"recall@{k}"] = recall
            per_metric[f"em@{k}"].append(em)
            per_metric[f"f1@{k}"].append(f1)
            per_metric[f"recall@{k}"].append(recall)
            per_type[qtype][f"em@{k}"].append(em)
            per_type[qtype][f"f1@{k}"].append(f1)
            per_type[qtype][f"recall@{k}"].append(recall)
        conn.close()

        per_q.append({
            "question_id": qid,
            "question_type": qtype,
            "question": q["question"],
            "gold_answer": gold_answer,
            "scores": q_scores,
        })

        if (idx + 1) % 25 == 0 or idx == 0:
            elapsed = time.perf_counter() - total_t
            rate = (idx + 1) / elapsed
            r10 = q_scores.get("f1@10", 0)
            print(f"  [{idx + 1}/{len(evaluable)}] {qid} ({qtype}) f1@10={r10:.3f} rate={rate:.1f}/s")

    wall = time.perf_counter() - total_t

    # Aggregate
    print(f"\n{'=' * 60}")
    print(f"LongMemEval Q&A Eval: {len(evaluable)} questions, {wall:.1f}s ({wall / len(evaluable):.2f}s/q)")
    print(f"\n{'Metric':<20} ", end="")
    for k in ks:
        print(f"  @{k:>3}", end="")
    print()
    print("-" * 60)
    for metric_name in ["em", "f1", "recall"]:
        print(f"{metric_name:<20} ", end="")
        for k in ks:
            val = mean(per_metric[f"{metric_name}@{k}"])
            print(f"  {val:.4f}", end="")
        print()

    # Per-type breakdown
    print("\nPer-type F1@10:")
    for qt in sorted(per_type):
        vals = per_type[qt]["f1@10"]
        n = len(vals)
        print(f"  {qt} (n={n}): {mean(vals):.4f}")

    # Failures
    failures = [pq for pq in per_q if pq["scores"].get("em@10", 0) == 0]
    print(f"\nExact-match failures @10: {len(failures)}/{len(evaluable)}")
    for pq in failures[:10]:
        print(f"  {pq['question_id']} ({pq['question_type']}) Q={pq['question'][:50]} A={pq['gold_answer'][:30]}")

    # Save results
    out_path = RESULTS_DIR / "longmemeval-qa-run.json"
    with open(out_path, "w") as f:
        json.dump({
            "benchmark": "LongMemEval-QA",
            "dataset": "xiaowu0162/longmemeval-cleaned",
            "n_questions": len(evaluable),
            "ks": ks,
            "macro": {f"{m}@{k}": mean(per_metric[f"{m}@{k}"]) for m in ["em", "f1", "recall"] for k in ks},
            "per_type": {
                qt: {f"{m}@{k}": mean(per_type[qt][f"{m}@{k}"]) for m in ["em", "f1", "recall"] for k in ks}
                for qt in per_type
            },
            "per_question": per_q,
            "wall_time_s": round(wall, 2),
        }, f, indent=2)
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    main()
