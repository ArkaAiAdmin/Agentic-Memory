"""
Phase 1: Diagnose the 22 retrieval failures from eval_full.json.

For each question with recall_all@10 < 1.0, print:
  - qid, question_type
  - question text
  - answer, answer_session_ids
  - haystack_dates[gold_index] (when the answer session occurred)
  - top-10 retrieved IDs (we have to re-run retrieval to get these)

Then categorize:
  - Temporal: explicit time expression in question
  - Implicit preference: no time, needs preference matching
  - Multi-hop: question synthesizes across 2+ sessions
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from retrieval import retrieve_for_question  # noqa: E402


def main() -> None:
    corpus_path = os.path.join(HERE, "longmemeval_s_cleaned.json")
    eval_path = os.path.join(HERE, "results/eval_full.json")

    print(f"Loading {corpus_path} ...", flush=True)
    with open(corpus_path) as f:
        corpus = json.load(f)
    with open(eval_path) as f:
        eval_data = json.load(f)

    # Build qid -> per_q map
    per_q_by_qid = {pq["question_id"]: pq for pq in eval_data["per_question"]}

    failures = []
    for q in corpus:
        qid = q["question_id"]
        if qid.endswith("_abs"):
            continue
        pq = per_q_by_qid.get(qid)
        if pq is None:
            continue
        if pq["scores"]["recall_all@10"] < 1.0:
            failures.append(q)

    print(f"\nFound {len(failures)} questions with recall_all@10 < 1.0", flush=True)

    # Print summary
    type_counts = Counter(q["question_type"] for q in failures)
    print("\nFailures by question_type:")
    for t, n in type_counts.most_common():
        print(f"  {t}: {n}")

    # Re-run retrieval for each failure to get top-10 IDs and dates (we don't
    # have them stored from the original run, but a single retry on 22 questions
    # is fast). Note: the harness is deterministic-ish, so top-10 should match
    # the original.
    print("\nRe-running retrieval on 22 failures to inspect top-10 ...", flush=True)
    detailed = []
    t0 = time.perf_counter()
    for q in failures:
        ranked, dbg = retrieve_for_question(
            question=q["question"],
            haystack_sessions=q["haystack_sessions"],
            haystack_session_ids=q["haystack_session_ids"],
        )
        gold = set(q["answer_session_ids"])
        # Find gold index in haystack
        sid_to_idx = {sid: i for i, sid in enumerate(q["haystack_session_ids"])}
        gold_indices = [sid_to_idx[s] for s in q["answer_session_ids"] if s in sid_to_idx]
        gold_dates = [q["haystack_dates"][i] for i in gold_indices]
        top10 = ranked[:10]
        top10_set = set(top10)
        hits = gold & top10_set
        detailed.append({
            "qid": q["question_id"],
            "type": q["question_type"],
            "question": q["question"],
            "question_date": q["question_date"],
            "answer": q["answer"],
            "n_gold": len(gold),
            "gold_session_ids": list(gold),
            "gold_dates": gold_dates,
            "top10": top10,
            "hits_in_top10": list(hits),
        })
    print(f"Re-run took {time.perf_counter() - t0:.1f}s", flush=True)

    # Print detail
    print("\n" + "=" * 80)
    print("FAILURE DETAIL")
    print("=" * 80)
    for d in detailed:
        print(f"\n--- {d['qid']} ({d['type']}) ---")
        print(f"Q ({d['question_date']}): {d['question']}")
        print(f"Answer: {d['answer']}")
        print(f"Gold sessions ({d['n_gold']}): {d['gold_session_ids']}")
        print(f"Gold dates: {d['gold_dates']}")
        print(f"Top-10: {d['top10']}")
        print(f"Hits in top-10: {d['hits_in_top10']}")

    # Categorize by simple pattern matching on question text
    temporal_patterns = [
        (r"\bN\s+days?\s+ago\b", "N days ago"),
        (r"\bN\s+weeks?\s+ago\b", "N weeks ago"),
        (r"\bN\s+months?\s+ago\b", "N months ago"),
        (r"\b(\d+)\s+days?\s+ago\b", r"\1 days ago"),
        (r"\b(\d+)\s+weeks?\s+ago\b", r"\1 weeks ago"),
        (r"\b(\d+)\s+months?\s+ago\b", r"\1 months ago"),
        (r"\byesterday\b", "yesterday"),
        (r"\blast\s+week\b", "last week"),
        (r"\bthis\s+week\b", "this week"),
        (r"\blast\s+month\b", "last month"),
        (r"\bthis\s+month\b", "this month"),
        (r"\bthe\s+past\s+(week|month|year|day)\b", r"past \1"),
        (r"\bthe\s+last\s+(week|month|year|day)\b", r"last \1"),
        (r"\bin\s+(January|February|March|April|May|June|July|August|September|October|November|December)\b", "in <month>"),
        (r"\bin\s+(\d{4})\b", "in <year>"),
        (r"\bthe\s+(first|second|third|fourth|fifth)\s+week\s+of\b", "nth week of"),
        (r"\bbefore\s+the\b", "before the"),
        (r"\bafter\s+the\b", "after the"),
        (r"\bduring\s+the\s+past\b", "during the past"),
        (r"\bover\s+the\s+past\b", "over the past"),
        (r"\brecently\b", "recently"),
    ]

    def detect_temporal(q: str) -> list[str]:
        found = []
        for pat, label in temporal_patterns:
            if re.search(pat, q, re.IGNORECASE):
                found.append(label)
        return found

    print("\n" + "=" * 80)
    print("CATEGORIZATION")
    print("=" * 80)
    cat_counts = Counter()
    for d in detailed:
        tem = detect_temporal(d["question"])
        is_multi_hop = d["n_gold"] >= 2
        labels = []
        if tem:
            labels.append("TEMPORAL(" + ",".join(tem) + ")")
        else:
            labels.append("NO_TIME_EXPR")
        if is_multi_hop:
            labels.append("MULTI_HOP")
        else:
            labels.append("SINGLE_GOLD")
        print(f"  {d['qid']:24s} [{d['type']:28s}] {' '.join(labels):60s} | {d['question'][:60]}")
        for lab in labels:
            cat_counts[lab] += 1

    print("\nLabel counts:")
    for k, v in cat_counts.most_common():
        print(f"  {k}: {v}")

    # Save detailed diagnosis
    out_path = os.path.join(HERE, "results/diagnosis_failures.json")
    with open(out_path, "w") as f:
        json.dump({
            "n_failures": len(detailed),
            "by_type": dict(type_counts),
            "by_label": dict(cat_counts),
            "failures": detailed,
        }, f, indent=2)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
