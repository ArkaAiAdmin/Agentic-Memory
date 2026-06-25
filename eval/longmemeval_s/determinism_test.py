"""
Determinism + date-prepend test.
For `4dfccbf8` and `8e91e7d9` (questions where bl shows gold[-1] but B shows rank 1):
- Re-run the SAME call 3 times → check if results match
- Re-run with haystack_dates (Approach A only, no boost) → see if dates alone move gold
- Re-run without dates, with boost only → see if boost alone moves gold
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from retrieval import retrieve_for_question, _extract_temporal_range  # noqa: E402


def main() -> None:
    with open(os.path.join(HERE, "longmemeval_s_cleaned.json")) as f:
        corpus = json.load(f)
    by_id = {q["question_id"]: q for q in corpus}

    targets = ["4dfccbf8", "8e91e7d9", "gpt4_8279ba03", "gpt4_4929293b", "eac54add"]

    for qid in targets:
        q = by_id[qid]
        gold = set(q["answer_session_ids"])
        trange = _extract_temporal_range(q["question"], q.get("question_date"))
        print(f"\n--- {qid}  gold={sorted(gold)}  range={trange} ---")

        # 1. baseline (no dates, no boost) — run 3 times
        for i in range(3):
            r, _ = retrieve_for_question(
                question=q["question"],
                haystack_sessions=q["haystack_sessions"],
                haystack_session_ids=q["haystack_session_ids"],
            )
            gold_pos = [r.index(s) + 1 if s in r else -1 for s in gold]
            print(f"  bl#{i+1}: ranks={gold_pos}, top5={r[:5]}")

        # 2. Approach A only (dates prepended, no boost)
        r, _ = retrieve_for_question(
            question=q["question"],
            haystack_sessions=q["haystack_sessions"],
            haystack_session_ids=q["haystack_session_ids"],
            haystack_dates=q["haystack_dates"],
        )
        gold_pos = [r.index(s) + 1 if s in r else -1 for s in gold]
        print(f"  A-only:  ranks={gold_pos}, top5={r[:5]}")

        # 3. B only (no dates, with boost at 1.5x and 5.0x)
        if trange:
            for boost in (1.5, 5.0, 10.0):
                r, _ = retrieve_for_question(
                    question=q["question"],
                    haystack_sessions=q["haystack_sessions"],
                    haystack_session_ids=q["haystack_session_ids"],
                    date_boost=boost,
                    temporal_range=trange,
                )
                gold_pos = [r.index(s) + 1 if s in r else -1 for s in gold]
                print(f"  B@{boost}x: ranks={gold_pos}, top5={r[:5]}")

        # 4. AB (dates + boost)
        if trange:
            for boost in (1.5, 5.0, 10.0):
                r, _ = retrieve_for_question(
                    question=q["question"],
                    haystack_sessions=q["haystack_sessions"],
                    haystack_session_ids=q["haystack_session_ids"],
                    haystack_dates=q["haystack_dates"],
                    date_boost=boost,
                    temporal_range=trange,
                )
                gold_pos = [r.index(s) + 1 if s in r else -1 for s in gold]
                print(f"  AB@{boost}x: ranks={gold_pos}, top5={r[:5]}")


if __name__ == "__main__":
    main()
