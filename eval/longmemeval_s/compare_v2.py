"""
Phase 5: Compare the 22 baseline failures across A, B, A+B.

For each of the 22 originally-failing questions, compare:
  - baseline recall@10 (from eval_full.json)
  - A recall@10 (from eval_v2_A.json)
  - B recall@10 (from eval_v2_B.json)
  - A+B recall@10 (from eval_v2_AB.json)
  - temporal_range (only B / A+B)
  - n_gold, question_type

Report per-type breakdown of each variant.
Report how many of the 22 are recovered (recall@10 jumps from <1.0 to 1.0).
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from statistics import mean

HERE = os.path.dirname(os.path.abspath(__file__))


def load(p: str) -> dict:
    with open(p) as f:
        return json.load(f)


def per_q_map(d: dict) -> dict[str, dict]:
    return {pq["question_id"]: pq for pq in d["per_question"]}


def macro_by_type(d: dict) -> dict[str, dict]:
    out: dict[str, dict] = defaultdict(lambda: {"n": 0, "r10": [], "r5": [], "r30": [], "r50": [], "ndcg10": []})
    for pq in d["per_question"]:
        t = pq["question_type"]
        out[t]["n"] += 1
        out[t]["r5"].append(pq["scores"]["recall_all@5"])
        out[t]["r10"].append(pq["scores"]["recall_all@10"])
        out[t]["r30"].append(pq["scores"]["recall_all@30"])
        out[t]["r50"].append(pq["scores"]["recall_all@50"])
        out[t]["ndcg10"].append(pq["scores"]["ndcg_any@10"])
    return {
        t: {
            "n": v["n"],
            "r5": round(mean(v["r5"]), 4),
            "r10": round(mean(v["r10"]), 4),
            "r30": round(mean(v["r30"]), 4),
            "r50": round(mean(v["r50"]), 4),
            "ndcg10": round(mean(v["ndcg10"]), 4),
        }
        for t, v in out.items()
    }


def main() -> None:
    baseline = load(os.path.join(HERE, "results/eval_full.json"))
    var_A = load(os.path.join(HERE, "results/eval_v2_A.json"))
    var_B = load(os.path.join(HERE, "results/eval_v2_B.json"))
    var_AB = load(os.path.join(HERE, "results/eval_v2_AB.json"))
    var_AB3 = load(os.path.join(HERE, "results/eval_v2_AB_boost3.json"))
    var_AB5 = load(os.path.join(HERE, "results/eval_v2_AB_boost5.json"))
    var_AB10 = load(os.path.join(HERE, "results/eval_v2_AB_boost10.json"))

    bl = per_q_map(baseline)
    A = per_q_map(var_A)
    B = per_q_map(var_B)
    AB = per_q_map(var_AB)
    AB3 = per_q_map(var_AB3)
    AB5 = per_q_map(var_AB5)
    AB10 = per_q_map(var_AB10)

    failing_qids = [qid for qid, pq in bl.items() if pq["scores"]["recall_all@10"] < 1.0]
    print(f"Original failures (baseline recall_all@10 < 1.0): {len(failing_qids)}")

    # Per-failure detail
    print("\n=== Per-failure comparison ===")
    recovered_by = {"A": [], "B": [], "AB@1.5x": [], "AB@3x": [], "AB@5x": [], "AB@10x": []}
    regressed_by = {"A": [], "B": [], "AB@1.5x": [], "AB@3x": [], "AB@5x": [], "AB@10x": []}
    for qid in failing_qids:
        bl_r = bl[qid]["scores"]["recall_all@10"]
        a_r = A[qid]["scores"]["recall_all@10"]
        b_r = B[qid]["scores"]["recall_all@10"]
        ab_r = AB[qid]["scores"]["recall_all@10"]
        ab3_r = AB3[qid]["scores"]["recall_all@10"]
        ab5_r = AB5[qid]["scores"]["recall_all@10"]
        ab10_r = AB10[qid]["scores"]["recall_all@10"]
        t = bl[qid]["question_type"]
        tr = AB5[qid].get("temporal_range")
        for label, r in [
            ("A", a_r), ("B", b_r), ("AB@1.5x", ab_r),
            ("AB@3x", ab3_r), ("AB@5x", ab5_r), ("AB@10x", ab10_r),
        ]:
            if bl_r < 1.0 and r >= 1.0:
                recovered_by[label].append(qid)
            elif bl_r >= 1.0 and r < 1.0:
                regressed_by[label].append(qid)
        print(f"  {qid:18s} [{t:28s}] bl={bl_r:.2f} A={a_r:.2f} B={b_r:.2f} "
              f"AB1.5x={ab_r:.2f} AB3x={ab3_r:.2f} AB5x={ab5_r:.2f} AB10x={ab10_r:.2f}  range={tr}")

    for k in recovered_by:
        print(f"\nRecovered by {k:8s}: {len(recovered_by[k])} {recovered_by[k]}")
        print(f"Regressed by {k:8s}: {len(regressed_by[k])} {regressed_by[k]}")

    # Union of recovered (any variant)
    all_recovered = set()
    for v in recovered_by.values():
        all_recovered |= set(v)
    print(f"\nUnion of recovered (any variant): {len(all_recovered)} {sorted(all_recovered)}")

    # Net count of new failures introduced
    for label, var in [
        ("A", A), ("B", B), ("AB", AB), ("AB@3", AB3), ("AB@5", AB5), ("AB@10", AB10)
    ]:
        new_fails = [qid for qid, pq in var.items() if pq["scores"]["recall_all@10"] < 1.0 and bl[qid]["scores"]["recall_all@10"] >= 1.0]
        print(f"New failures in {label}: {len(new_fails)} {new_fails}")

    # Per-type table
    print("\n=== Per-type macro table (recall_all@10) ===")
    bl_t = macro_by_type(baseline)
    a_t = macro_by_type(var_A)
    b_t = macro_by_type(var_B)
    ab_t = macro_by_type(var_AB)
    ab3_t = macro_by_type(var_AB3)
    ab5_t = macro_by_type(var_AB5)
    ab10_t = macro_by_type(var_AB10)
    types = sorted(bl_t.keys(), key=lambda t: -bl_t[t]["n"])
    print(f"{'type':30s} {'n':>3s} | {'baseline':>9s} {'A':>7s} {'B':>7s} {'AB1.5':>7s} {'AB3':>7s} {'AB5':>7s} {'AB10':>7s} | {'AB5 ndcg10':>11s}")
    for t in types:
        print(
            f"{t:30s} {bl_t[t]['n']:>3d} | "
            f"{bl_t[t]['r10']*100:>8.2f}% "
            f"{a_t[t]['r10']*100:>6.2f}% "
            f"{b_t[t]['r10']*100:>6.2f}% "
            f"{ab_t[t]['r10']*100:>6.2f}% "
            f"{ab3_t[t]['r10']*100:>6.2f}% "
            f"{ab5_t[t]['r10']*100:>6.2f}% "
            f"{ab10_t[t]['r10']*100:>6.2f}% | "
            f"{ab5_t[t]['ndcg10']:>11.4f}"
        )

    # Macro overall
    print(f"\n{'OVERALL':30s} {len(bl):>3d} | "
          f"{baseline['macro']['recall_all@10']*100:>8.2f}% "
          f"{var_A['macro']['recall_all@10']*100:>6.2f}% "
          f"{var_B['macro']['recall_all@10']*100:>6.2f}% "
          f"{var_AB['macro']['recall_all@10']*100:>6.2f}% "
          f"{var_AB3['macro']['recall_all@10']*100:>6.2f}% "
          f"{var_AB5['macro']['recall_all@10']*100:>6.2f}% "
          f"{var_AB10['macro']['recall_all@10']*100:>6.2f}% | "
          f"{var_AB5['macro']['ndcg_any@10']:>11.4f}")

    # Latency
    print("\n=== Latency ===")
    for label, var in [
        ("baseline", baseline), ("A", var_A), ("B", var_B),
        ("AB@1.5", var_AB), ("AB@3", var_AB3), ("AB@5", var_AB5), ("AB@10", var_AB10),
    ]:
        lat = [pq["elapsed_s"] for pq in var["per_question"]]
        lat_sorted = sorted(lat)
        n = len(lat_sorted)
        mean_lat = sum(lat_sorted) / n
        p50 = lat_sorted[n // 2]
        p95 = lat_sorted[int(n * 0.95)]
        p99 = lat_sorted[int(n * 0.99)]
        print(f"  {label:9s}: mean={mean_lat:.4f}s p50={p50:.4f}s p95={p95:.4f}s p99={p99:.4f}s total={var['total_elapsed_s']}s")

    # Save detailed comparison
    out = {
        "config": {
            "n_questions": len(bl),
            "n_baseline_failures": len(failing_qids),
        },
        "recovered": {k: v for k, v in recovered_by.items()},
        "regressed": {k: v for k, v in regressed_by.items()},
        "per_failure": [
            {
                "qid": qid,
                "type": bl[qid]["question_type"],
                "baseline": bl[qid]["scores"]["recall_all@10"],
                "A": A[qid]["scores"]["recall_all@10"],
                "B": B[qid]["scores"]["recall_all@10"],
                "AB@1.5x": AB[qid]["scores"]["recall_all@10"],
                "AB@3x": AB3[qid]["scores"]["recall_all@10"],
                "AB@5x": AB5[qid]["scores"]["recall_all@10"],
                "AB@10x": AB10[qid]["scores"]["recall_all@10"],
                "temporal_range_AB@5x": AB5[qid].get("temporal_range"),
            }
            for qid in failing_qids
        ],
        "macro": {
            "baseline": baseline["macro"],
            "A": var_A["macro"],
            "B": var_B["macro"],
            "AB@1.5x": var_AB["macro"],
            "AB@3x": var_AB3["macro"],
            "AB@5x": var_AB5["macro"],
            "AB@10x": var_AB10["macro"],
        },
        "per_type": {
            t: {
                "n": bl_t[t]["n"],
                "baseline_r10": bl_t[t]["r10"],
                "A_r10": a_t[t]["r10"],
                "B_r10": b_t[t]["r10"],
                "AB1.5x_r10": ab_t[t]["r10"],
                "AB3x_r10": ab3_t[t]["r10"],
                "AB5x_r10": ab5_t[t]["r10"],
                "AB10x_r10": ab10_t[t]["r10"],
                "AB5x_ndcg10": ab5_t[t]["ndcg10"],
            }
            for t in types
        },
        "latency": {
            "baseline": {
                "mean_s": sum(pq["elapsed_s"] for pq in baseline["per_question"]) / len(baseline["per_question"]),
                "total_s": baseline["total_elapsed_s"],
            },
            "A": {
                "mean_s": sum(pq["elapsed_s"] for pq in var_A["per_question"]) / len(var_A["per_question"]),
                "total_s": var_A["total_elapsed_s"],
            },
            "B": {
                "mean_s": sum(pq["elapsed_s"] for pq in var_B["per_question"]) / len(var_B["per_question"]),
                "total_s": var_B["total_elapsed_s"],
            },
            "AB@1.5x": {
                "mean_s": sum(pq["elapsed_s"] for pq in var_AB["per_question"]) / len(var_AB["per_question"]),
                "total_s": var_AB["total_elapsed_s"],
            },
            "AB@3x": {
                "mean_s": sum(pq["elapsed_s"] for pq in var_AB3["per_question"]) / len(var_AB3["per_question"]),
                "total_s": var_AB3["total_elapsed_s"],
            },
            "AB@5x": {
                "mean_s": sum(pq["elapsed_s"] for pq in var_AB5["per_question"]) / len(var_AB5["per_question"]),
                "total_s": var_AB5["total_elapsed_s"],
            },
            "AB@10x": {
                "mean_s": sum(pq["elapsed_s"] for pq in var_AB10["per_question"]) / len(var_AB10["per_question"]),
                "total_s": var_AB10["total_elapsed_s"],
            },
        },
    }
    with open(os.path.join(HERE, "results/comparison_v2.json"), "w") as f:
        json.dump(out, f, indent=2)
    print("\nWrote results/comparison_v2.json")


if __name__ == "__main__":
    main()
