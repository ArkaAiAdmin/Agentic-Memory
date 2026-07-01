"""
LongMemEval_S BM25-only baseline driver.

Same as run_eval.py but passes use_ce=False to retrieve_for_question,
producing a pure BM25 (FTS5) ranking. This is the "naive RAG" baseline
we compare against.

Usage:
  ~/.config/agentic-memory/venv/bin/python run_eval_bm25_only.py
"""
from __future__ import annotations

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
CORPUS = os.path.join(HERE, "longmemeval_s_cleaned.json")
OUT = os.path.join(HERE, "results", "eval_bm25_only.json")


def is_evaluable(entry: dict) -> bool:
    return not entry["question_id"].endswith("_abs")


def main() -> None:
    print(f"Loading {CORPUS} ...", flush=True)
    with open(CORPUS) as f:
        corpus = json.load(f)
    evaluable = [q for q in corpus if is_evaluable(q)]
    print(f"Loaded {len(corpus)} questions, {len(evaluable)} evaluable.", flush=True)

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
            use_ce=False,
        )
        scores = compute_all_k(ranked, gold, ks=KS)
        per_q.append({
            "question_id": qid,
            "question_type": q["question_type"],
            "n_sessions": dbg["n_sessions"],
            "bm25_hits": dbg["bm25_hits"],
            "elapsed_s": dbg["elapsed_s"],
            "n_gold": len(gold),
            "scores": scores,
            "top_10_retrieved": ranked[:10],
        })
        for k, v in scores.items():
            per_metric[k].append(v)
        if (idx + 1) % 50 == 0 or idx == 0 or idx == len(evaluable) - 1:
            print(
                f"  [{idx + 1}/{len(evaluable)}] {qid} ({q['question_type']}) "
                f"recall@10={scores['recall_any@10']:.2f} "
                f"ndcg@10={scores['ndcg_any@10']:.2f} "
                f"latency={dbg['elapsed_s']}s",
                flush=True,
            )

    total_elapsed = time.perf_counter() - total_t
    macro = {k: round(mean(v), 4) for k, v in per_metric.items()}

    out = {
        "config": {"ks": list(KS), "n_questions": len(evaluable), "mode": "bm25_only"},
        "macro": macro,
        "per_question": per_q,
        "total_elapsed_s": round(total_elapsed, 2),
    }
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote {OUT}")
    print(f"Total wall time: {total_elapsed:.1f}s")
    print("Macro (BM25 only):")
    for k, v in macro.items():
        print(f"  {k}: {v:.4f}")


if __name__ == "__main__":
    main()
