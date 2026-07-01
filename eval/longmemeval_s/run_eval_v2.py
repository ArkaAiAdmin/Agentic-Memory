"""
LongMemEval_S retrieval eval with time-aware expansion (A, B, A+B).

A (implicit): prepend "Session date: <date>" to each doc's indexed text
B (explicit): parse temporal expression in question, boost sessions in the
              computed range
A+B: both

Usage:
  ~/.config/agentic-memory/venv/bin/python run_eval_v2.py \\
      --input eval/longmemeval_s/longmemeval_s_cleaned.json \\
      --output eval/longmemeval_s/results/eval_A.json \\
      --variant A \\
      --limit 5
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
from retrieval import (  # noqa: E402
    retrieve_for_question,
    _extract_temporal_range,
)

KS = (5, 10, 30, 50)
DEFAULT_BOOST = 1.5  # multiplier when session is in temporal range


def is_evaluable(entry: dict) -> bool:
    return not entry["question_id"].endswith("_abs")


def load_corpus(path: str) -> list[dict]:
    with open(path) as f:
        return json.load(f)


def run(
    corpus: list[dict],
    variant: str,
    limit: int | None = None,
    boost: float = DEFAULT_BOOST,
) -> dict:
    assert variant in ("A", "B", "AB"), f"unknown variant: {variant}"
    use_dates_in_text = variant in ("A", "AB")
    use_explicit_boost = variant in ("B", "AB")

    evaluable = [q for q in corpus if is_evaluable(q)]
    if limit is not None:
        evaluable = evaluable[:limit]

    per_q: list[dict] = []
    per_metric: dict[str, list[float]] = defaultdict(list)
    n_with_temporal_range = 0
    total_t = time.perf_counter()
    for idx, q in enumerate(evaluable):
        qid = q["question_id"]
        gold = q["answer_session_ids"]

        # Build kwargs for retrieve_for_question
        kwargs = dict(
            question=q["question"],
            haystack_sessions=q["haystack_sessions"],
            haystack_session_ids=q["haystack_session_ids"],
        )
        if use_dates_in_text:
            kwargs["haystack_dates"] = q["haystack_dates"]

        if use_explicit_boost:
            trange = _extract_temporal_range(q["question"], q.get("question_date"))
            if trange is not None:
                kwargs["date_boost"] = boost
                kwargs["temporal_range"] = trange
                n_with_temporal_range += 1

        ranked, dbg = retrieve_for_question(**kwargs)
        dbg["temporal_range"] = (
            kwargs.get("temporal_range") if use_explicit_boost else None
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
            "temporal_range": dbg["temporal_range"],
            "scores": scores,
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

    return {
        "config": {
            "variant": variant,
            "ks": list(KS),
            "n_questions": len(evaluable),
            "use_dates_in_text": use_dates_in_text,
            "use_explicit_boost": use_explicit_boost,
            "boost_factor": boost,
            "n_with_temporal_range": n_with_temporal_range,
        },
        "macro": macro,
        "per_question": per_q,
        "total_elapsed_s": round(total_elapsed, 2),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--variant", required=True, choices=["A", "B", "AB"])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--boost", type=float, default=DEFAULT_BOOST)
    args = p.parse_args()

    print(f"Variant: {args.variant}  boost={args.boost}", flush=True)
    print(f"Loading {args.input} ...", flush=True)
    corpus = load_corpus(args.input)
    n_eval = sum(1 for q in corpus if is_evaluable(q))
    print(f"Loaded {len(corpus)} questions, {n_eval} evaluable.", flush=True)

    t0 = time.perf_counter()
    results = run(corpus, variant=args.variant, limit=args.limit, boost=args.boost)
    print(f"Done. Total wall time: {time.perf_counter() - t0:.1f}s", flush=True)
    print("Macro-averaged metrics:")
    for k, v in results["macro"].items():
        print(f"  {k}: {v:.4f}")
    print(f"Questions with extracted temporal range: "
          f"{results['config']['n_with_temporal_range']} / {results['config']['n_questions']}")

    with open(args.output, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
