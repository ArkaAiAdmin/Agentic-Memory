"""
Compare gold-session ranks: baseline vs Approach B (boost 5.0, 10.0).
For each of the 10 failures where a temporal range was extracted,
show how the gold ranks move under different boost factors.
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from retrieval import (  # noqa: E402
    retrieve_for_question,
    _extract_temporal_range,
)


def gold_ranks_in(ranked: list[str], gold: set[str]) -> list[int]:
    """1-indexed ranks of each gold session; -1 if not in list."""
    out = []
    for g in gold:
        try:
            out.append(ranked.index(g) + 1)
        except ValueError:
            out.append(-1)
    return out


def main() -> None:
    with open(os.path.join(HERE, "longmemeval_s_cleaned.json")) as f:
        corpus = json.load(f)
    with open(os.path.join(HERE, "results/eval_full.json")) as f:
        eval_data = json.load(f)
    per_q = {pq["question_id"]: pq for pq in eval_data["per_question"]}
    failing = [q for q in corpus
               if not q["question_id"].endswith("_abs")
               and per_q[q["question_id"]]["scores"]["recall_all@10"] < 1.0]
    print(f"Re-running {len(failing)} failures ...")

    print(f"\n{'qid':18s} {'bl':>10s} {'B@1.5x':>10s} {'B@3x':>10s} {'B@5x':>10s} {'B@10x':>10s} {'AB@5x':>10s} {'range':>22s}")
    for q in failing:
        qid = q["question_id"]
        gold = set(q["answer_session_ids"])
        trange = _extract_temporal_range(q["question"], q.get("question_date"))
        trange_str = f"{trange[0]}..{trange[1]}" if trange else "None"

        # baseline (no A, no B)
        bl_ranked, _ = retrieve_for_question(
            question=q["question"],
            haystack_sessions=q["haystack_sessions"],
            haystack_session_ids=q["haystack_session_ids"],
        )
        bl_ranks = gold_ranks_in(bl_ranked, gold)
        max(bl_ranks) if bl_ranks else -1

        # Approach B at different boost levels
        rows = []
        for boost in (1.5, 3.0, 5.0, 10.0):
            r, _ = retrieve_for_question(
                question=q["question"],
                haystack_sessions=q["haystack_sessions"],
                haystack_session_ids=q["haystack_session_ids"],
                haystack_dates=q["haystack_dates"],
                date_boost=boost,
                temporal_range=trange,
            )
            ranks = gold_ranks_in(r, gold)
            rows.append(max(ranks) if ranks else -1)

        # AB at 5x
        r, _ = retrieve_for_question(
            question=q["question"],
            haystack_sessions=q["haystack_sessions"],
            haystack_session_ids=q["haystack_session_ids"],
            haystack_dates=q["haystack_dates"],
            date_boost=5.0,
            temporal_range=trange,
        )
        ab_ranks = gold_ranks_in(r, gold)
        ab_worst = max(ab_ranks) if ab_ranks else -1

        bl_str = ",".join(str(x) for x in bl_ranks)
        print(f"  {qid:18s} {bl_str:>10s} "
              f"{rows[0]:>10d} {rows[1]:>10d} {rows[2]:>10d} {rows[3]:>10d} {ab_worst:>10d} "
              f"{trange_str:>22s}")


if __name__ == "__main__":
    main()
