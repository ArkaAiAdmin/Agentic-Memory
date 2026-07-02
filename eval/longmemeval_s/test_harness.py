"""
Test harness for the LongMemEval_S retrieval pipeline.

Runs on the first 5 non-abstention questions and prints:
  - per-question gold ids, top-10 retrieved ids, and metrics
  - macro average across the 5
  - total wall time

Confirms the gold sessions are surfacing in the top-30 (the sanity check
described in the protocol). Does NOT run the full 470-question eval.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from metrics import recall_all_at_k, recall_any_at_k, ndcg_any_at_k  # noqa: E402
from retrieval import retrieve_for_question  # noqa: E402


CORPUS_PATH = os.path.join(HERE, "longmemeval_s_cleaned.json")
N_TEST = 5
KS = (5, 10, 30, 50)


def main() -> None:
    print(f"Loading {CORPUS_PATH} ...", flush=True)
    with open(CORPUS_PATH) as f:
        corpus = json.load(f)
    evaluable = [q for q in corpus if not q["question_id"].endswith("_abs")]
    sample = evaluable[:N_TEST]
    print(
        f"Total questions: {len(corpus)} | evaluable: {len(evaluable)} | testing: {N_TEST}",
        flush=True,
    )
    print(f"Sampling qids: {[q['question_id'] for q in sample]}", flush=True)
    print()

    metrics_per_q: list[dict] = []
    rankings: list[list[str]] = []
    gold_lists: list[list[str]] = []
    for idx, q in enumerate(sample):
        qid = q["question_id"]
        qtype = q["question_type"]
        question = q["question"]
        gold = q["answer_session_ids"]
        print(f"=== Q{idx} {qid} ({qtype}) ===")
        print(f"  question: {question}")
        print(f"  answer: {q['answer']}")
        print(f"  gold_session_ids ({len(gold)}): {gold}")
        print(f"  haystack_sessions: {len(q['haystack_sessions'])}")

        ranked, dbg = retrieve_for_question(
            question=question,
            haystack_sessions=q["haystack_sessions"],
            haystack_session_ids=q["haystack_session_ids"],
        )
        rankings.append(ranked)
        gold_lists.append(gold)

        top10 = ranked[:10]
        top30 = ranked[:30]
        gold_in_top30 = [g for g in gold if g in top30]
        gold_in_top5 = [g for g in gold if g in ranked[:5]]
        print(f"  top-10 retrieved: {top10}")
        print(f"  gold-in-top-5:  {gold_in_top5}")
        print(f"  gold-in-top-30: {gold_in_top30}")
        print(
            f"  bm25_hits={dbg['bm25_hits']}  ce_scored={dbg['ce_scored']}  "
            f"elapsed={dbg['elapsed_s']}s"
        )

        per_q = {}
        for k in KS:
            per_q[f"recall_all@{k}"] = recall_all_at_k(ranked, gold, k)
            per_q[f"recall_any@{k}"] = recall_any_at_k(ranked, gold, k)
            per_q[f"ndcg_any@{k}"] = ndcg_any_at_k(ranked, gold, k)
        per_q["latency_s"] = dbg["elapsed_s"]
        metrics_per_q.append(per_q)

        line = (
            f"  Q{idx} ({qtype}): "
            f"recall@5={per_q['recall_any@5']:.2f} "
            f"recall@10={per_q['recall_any@10']:.2f} "
            f"recall@30={per_q['recall_any@30']:.2f} "
            f"ndcg@10={per_q['ndcg_any@10']:.2f} "
            f"(latency {per_q['latency_s']}s)"
        )
        print(line)
        print()

    print("=== Average across 5 ===")
    avg: dict[str, float] = {}
    for k in KS:
        avg[f"recall_all@{k}"] = statistics.mean(
            m[f"recall_all@{k}"] for m in metrics_per_q
        )
        avg[f"recall_any@{k}"] = statistics.mean(
            m[f"recall_any@{k}"] for m in metrics_per_q
        )
        avg[f"ndcg_any@{k}"] = statistics.mean(
            m[f"ndcg_any@{k}"] for m in metrics_per_q
        )
    avg["latency_s"] = statistics.mean(m["latency_s"] for m in metrics_per_q)
    for k, v in avg.items():
        print(f"  {k}: {v:.4f}")

    # Sanity check verdict — reuse the rankings we already computed above.
    per_q_sanity = []
    for qid, ranked, gold in zip(
        (q["question_id"] for q in sample), rankings, gold_lists
    ):
        top30 = ranked[:30]
        missing = [g for g in gold if g not in top30]
        per_q_sanity.append((qid, missing))
    all_gold_in_top30 = all(len(m) == 0 for _, m in per_q_sanity)
    for qid, missing in per_q_sanity:
        if missing:
            print(f"  MISS in top-30 for {qid}: {missing}")
    print()
    print("=== Sanity check ===")
    print(f"All 5 questions have all gold sessions in top-30: {all_gold_in_top30}")


if __name__ == "__main__":
    t0 = time.perf_counter()
    main()
    print(f"\nTotal wall time: {time.perf_counter() - t0:.1f}s")
