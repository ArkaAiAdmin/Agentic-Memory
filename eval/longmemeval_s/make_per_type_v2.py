"""
Generate per_question.csv and per_type.csv for the v2 winner (AB@5x).
Writes to results/per_question_v2.csv and results/per_type_v2.csv.
"""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from statistics import mean

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    with open(os.path.join(HERE, "results/eval_full_v2.json")) as f:
        v2 = json.load(f)
    with open(os.path.join(HERE, "results/eval_full.json")) as f:
        bl = json.load(f)

    # Flat per-question CSV
    pq_v2 = {pq["question_id"]: pq for pq in v2["per_question"]}
    pq_bl = {pq["question_id"]: pq for pq in bl["per_question"]}
    out_pq = os.path.join(HERE, "results/per_question_v2.csv")
    with open(out_pq, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "qid", "type", "n_sessions", "n_gold",
            "baseline_r10", "v2_r10", "delta_r10",
            "baseline_ndcg10", "v2_ndcg10", "delta_ndcg10",
            "elapsed_s", "temporal_range",
        ])
        for qid in sorted(pq_v2.keys()):
            v2_pq = pq_v2[qid]
            bl_pq = pq_bl[qid]
            r10 = v2_pq["scores"]["recall_all@10"]
            br10 = bl_pq["scores"]["recall_all@10"]
            n10 = v2_pq["scores"]["ndcg_any@10"]
            bn10 = bl_pq["scores"]["ndcg_any@10"]
            tr = v2_pq.get("temporal_range")
            tr_s = f"{tr[0]}..{tr[1]}" if tr else ""
            w.writerow([
                qid, v2_pq["question_type"], v2_pq["n_sessions"], v2_pq["n_gold"],
                f"{br10:.4f}", f"{r10:.4f}", f"{r10 - br10:+.4f}",
                f"{bn10:.4f}", f"{n10:.4f}", f"{n10 - bn10:+.4f}",
                f"{v2_pq['elapsed_s']:.4f}", tr_s,
            ])
    print(f"Wrote {out_pq}")

    # Per-type CSV
    by_type_v2 = defaultdict(list)
    by_type_bl = defaultdict(list)
    n_by_type = defaultdict(int)
    for qid, pq in pq_v2.items():
        t = pq["question_type"]
        by_type_v2[t].append(pq["scores"])
        by_type_bl[t].append(pq_bl[qid]["scores"])
        n_by_type[t] += 1

    out_pt = os.path.join(HERE, "results/per_type_v2.csv")
    with open(out_pt, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "system", "type", "n",
            "recall_all@5", "recall_all@10", "recall_all@30", "recall_all@50",
            "recall_any@10",
            "ndcg_any@10",
        ])
        for t in sorted(by_type_v2.keys(), key=lambda k: -n_by_type[k]):
            n = n_by_type[t]
            row_v2 = by_type_v2[t]
            row_bl = by_type_bl[t]
            w.writerow([
                "baseline", t, n,
                f"{mean(s['recall_all@5'] for s in row_bl):.4f}",
                f"{mean(s['recall_all@10'] for s in row_bl):.4f}",
                f"{mean(s['recall_all@30'] for s in row_bl):.4f}",
                f"{mean(s['recall_all@50'] for s in row_bl):.4f}",
                f"{mean(s['recall_any@10'] for s in row_bl):.4f}",
                f"{mean(s['ndcg_any@10'] for s in row_bl):.4f}",
            ])
            w.writerow([
                "v2_AB5x", t, n,
                f"{mean(s['recall_all@5'] for s in row_v2):.4f}",
                f"{mean(s['recall_all@10'] for s in row_v2):.4f}",
                f"{mean(s['recall_all@30'] for s in row_v2):.4f}",
                f"{mean(s['recall_all@50'] for s in row_v2):.4f}",
                f"{mean(s['recall_any@10'] for s in row_v2):.4f}",
                f"{mean(s['ndcg_any@10'] for s in row_v2):.4f}",
            ])
    print(f"Wrote {out_pt}")

    # Print summary
    print("\n--- AB@5x vs baseline (per-type) ---")
    for t in sorted(by_type_v2.keys(), key=lambda k: -n_by_type[k]):
        v2s = by_type_v2[t]
        bls = by_type_bl[t]
        v2r10 = mean(s["recall_all@10"] for s in v2s)
        blr10 = mean(s["recall_all@10"] for s in bls)
        v2n10 = mean(s["ndcg_any@10"] for s in v2s)
        bln10 = mean(s["ndcg_any@10"] for s in bls)
        print(f"  {t:30s} n={n_by_type[t]:>3d}  r10: {blr10*100:>6.2f}% -> {v2r10*100:>6.2f}% ({v2r10-blr10:+.4f})  ndcg10: {bln10:.4f} -> {v2n10:.4f} ({v2n10-bln10:+.4f})")


if __name__ == "__main__":
    main()
