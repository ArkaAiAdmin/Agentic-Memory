"""
LongMemEval_S full retrieval eval driver.

Loops over the 470 non-abstention questions, runs BM25 + CE per question,
and macro-averages recall_all / recall_any / ndcg_any at k=5,10,30,50.

Usage:
  ~/.config/agentic-memory/venv/bin/python run_eval.py \\
      --input /path/to/longmemeval_s_cleaned.json \\
      --output /path/to/results.json \\
      --limit 5     # optional, for partial runs (test harness uses this)

Notes:
  - The cross-encoder is loaded once and reused across questions.
  - Per-question state is small (~48 docs); we never touch the prod DB.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from statistics import mean

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from infra.metrics import compute_all_k  # noqa: E402
from retrieval import retrieve_for_question  # noqa: E402


KS = (5, 10, 30, 50)


def is_evaluable(entry: dict) -> bool:
    return not entry["question_id"].endswith("_abs")


def load_corpus(path: str) -> list[dict]:
    with open(path) as f:
        return json.load(f)


def run(corpus: list[dict], limit: int | None = None) -> dict:
    evaluable = [q for q in corpus if is_evaluable(q)]
    if limit is not None:
        evaluable = evaluable[:limit]

    per_q: list[dict] = []
    per_metric: dict[str, list[float]] = defaultdict(list)
    total_t = time.perf_counter()
    for idx, q in enumerate(evaluable):
        qid = q["question_id"]
        gold = q["answer_session_ids"]
        ranked, dbg = retrieve_for_question(
            question=q["question"],
            haystack_sessions=q["haystack_sessions"],
            haystack_session_ids=q["haystack_session_ids"],
        )
        scores = compute_all_k(ranked, gold, ks=KS)
        per_q.append({
            "question_id": qid,
            "question_type": q["question_type"],
            "n_sessions": dbg["n_sessions"],
            "bm25_hits": dbg["bm25_hits"],
            "ce_scored": dbg["ce_scored"],
            "elapsed_s": dbg["elapsed_s"],
            "n_gold": len(gold),
            "scores": scores,
        })
        for k, v in scores.items():
            per_metric[k].append(v)
        if (idx + 1) % 25 == 0 or idx == 0 or idx == len(evaluable) - 1:
            print(
                f"  [{idx + 1}/{len(evaluable)}] {qid} ({q['question_type']}) "
                f"recall@10={scores['recall_any@10']:.2f} "
                f"ndcg@10={scores['ndcg_any@10']:.2f} "
                f"latency={dbg['elapsed_s']}s",
                flush=True,
            )

    total_elapsed = time.perf_counter() - total_t
    macro = {k: round(mean(v), 4) for k, v in per_metric.items()}

    return {
        "config": {"ks": list(KS), "n_questions": len(evaluable)},
        "macro": macro,
        "per_question": per_q,
        "total_elapsed_s": round(total_elapsed, 2),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    print(f"Loading {args.input} ...", flush=True)
    corpus = load_corpus(args.input)
    n_eval = sum(1 for q in corpus if is_evaluable(q))
    print(f"Loaded {len(corpus)} questions, {n_eval} evaluable.", flush=True)

    t0 = time.perf_counter()
    results = run(corpus, limit=args.limit)
    print(f"Done. Total wall time: {time.perf_counter() - t0:.1f}s", flush=True)
    print("Macro-averaged metrics:")
    for k, v in results["macro"].items():
        print(f"  {k}: {v:.4f}")

    with open(args.output, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
