"""
For each of the 22 failures, dig into the candidate pool:
- Where does the gold session rank in the BM25 candidate pool (top-50)?
- Where does it rank in the final ranking?
- With boost, where does it go?

This tells us whether Approach B's failure is:
  - Gold not in candidate pool (impossible to recover with boost)
  - Gold in pool but below top-10 (boost could help, just not enough)
  - Gold in top-10 already (no issue)
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


def main() -> None:
    corpus_path = os.path.join(HERE, "longmemeval_s_cleaned.json")
    baseline_path = os.path.join(HERE, "results/eval_full.json")
    with open(corpus_path) as f:
        corpus = json.load(f)
    with open(baseline_path) as f:
        eval_data = json.load(f)
    per_q = {pq["question_id"]: pq for pq in eval_data["per_question"]}

    failing_qids = [qid for qid, pq in per_q.items() if pq["scores"]["recall_all@10"] < 1.0]
    failing = [q for q in corpus if q["question_id"] in failing_qids]
    print(f"Investigating {len(failing)} failures ...")

    print(f"\n{'qid':18s} {'type':28s} {'gold#':>5s} {'poolRank':>9s} {'finalRank':>10s} {'boostedRank':>13s} {'range':>22s}")
    for q in failing:
        qid = q["question_id"]
        gold_set = set(q["answer_session_ids"])
        sid_to_idx = {sid: i for i, sid in enumerate(q["haystack_session_ids"])}
        [sid_to_idx[s] for s in q["answer_session_ids"] if s in sid_to_idx]
        trange = _extract_temporal_range(q["question"], q.get("question_date"))
        trange_str = f"{trange[0]}..{trange[1]}" if trange else "None"

        # Run with extra-candidate-pool to see where gold lands in pool
        ranked_pool, _ = retrieve_for_question(
            question=q["question"],
            haystack_sessions=q["haystack_sessions"],
            haystack_session_ids=q["haystack_session_ids"],
            haystack_dates=q["haystack_dates"],
            candidate_pool=50,
        )
        # final ranking: same as ranked_pool since we keep all 50
        # boosted: re-score
        ranked_boost, _ = retrieve_for_question(
            question=q["question"],
            haystack_sessions=q["haystack_sessions"],
            haystack_session_ids=q["haystack_session_ids"],
            haystack_dates=q["haystack_dates"],
            candidate_pool=50,
            date_boost=10.0,  # extreme boost to see if it can EVER push gold into top-10
            temporal_range=trange,
        )

        # Find worst (lowest-ranked) gold position in pool
        worst_pool = None
        worst_final = None
        worst_boost = None
        for i, sid in enumerate(ranked_pool):
            if sid in gold_set:
                if worst_pool is None:
                    worst_pool = i + 1  # 1-indexed
        for i, sid in enumerate(ranked_boost):
            if sid in gold_set:
                if worst_boost is None:
                    worst_boost = i + 1
        # Final rank in baseline eval was 10-pool (candidate_pool=50, but recall@10
        # only counts top-10). If worst_pool > 10, gold was outside top-10.
        worst_final = worst_pool  # same candidate pool
        print(f"  {qid:18s} {q['question_type']:28s} {len(gold_set):>5d} {str(worst_pool):>9s} {str(worst_final):>10s} {str(worst_boost):>13s} {trange_str:>22s}")


if __name__ == "__main__":
    main()
