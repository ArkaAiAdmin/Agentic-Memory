#!/usr/bin/env python3
"""Standalone golden eval — runs directly against a pre-built DB."""
import json, os, sys, time
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

eval_db = Path(os.environ.get("EVAL_DB", "")) or (INSTALL_DIR / "eval" / "prebuilt_db_path.txt")
if eval_db.suffix == ".txt":
    eval_db = Path(eval_db.read_text().strip())
os.environ["MEMORY_DB_PATH"] = str(eval_db)

from search.orchestrator import search_memories

golden = json.load(open(INSTALL_DIR / "eval" / "real_memory_golden_v2.json"))
targets = golden["targets"]
test_cases = golden["test_cases"]

def recall_at_k(retrieved, expected, k):
    if not expected:
        return 1.0
    hits = len(set(expected) & set(retrieved[:k]))
    return hits / len(expected)

def mrr(retrieved, expected):
    for i, d in enumerate(retrieved):
        if d in expected:
            return 1.0 / (i + 1)
    return 0.0

results = {"recall_at_10": [], "mrr": []}
cat_results = {}

print(f"\nRunning {len(test_cases)} golden queries...\n")
for i, tc in enumerate(test_cases):
    query = tc["query"]
    expected = tc["expected"]
    cat = tc.get("category", "unknown")

    as_of = None
    if cat == "temporal":
        import re, calendar
        for eid in expected:
            m = re.search(r'(\d{4})-(\d{2})-(\d{2})', eid)
            if m:
                as_of = calendar.timegm(time.strptime(m.group(0), "%Y-%m-%d")) + 86400
                break

    t0 = time.time()
    try:
        res = search_memories(query=query, db_path=eval_db, limit=50, hybrid=True, rerank=True, as_of=as_of)
        retrieved = [r.get("id", "") for r in res.get("results", [])] if isinstance(res, dict) else []
    except Exception as e:
        print(f"  ERROR [{cat}] {query[:60]}: {e}")
        retrieved = []
    lat = (time.time() - t0) * 1000

    r10 = recall_at_k(retrieved, expected, 10)
    m = mrr(retrieved, expected)
    results["recall_at_10"].append(r10)
    results["mrr"].append(m)

    cat_results.setdefault(cat, {"r10": [], "lat": []})
    cat_results[cat]["r10"].append(r10)
    cat_results[cat]["lat"].append(lat)

    passed = r10 >= tc.get("min_recall_at_10", 0.8)
    if not passed:
        print(f"  FAIL [{cat:15}] recall@10={r10:.3f}  query={query[:60]}")

avg_r10 = sum(results["recall_at_10"]) / len(results["recall_at_10"])
avg_mrr = sum(results["mrr"]) / len(results["mrr"])

print("\n" + "=" * 70)
print("GOLDEN EVAL RESULTS")
print("=" * 70)
print(f"  Overall recall@10: {avg_r10:.4f}  (target={targets.get('recall_at_10', 0.92)})  {'PASS' if avg_r10 >= targets.get('recall_at_10', 0.92) else 'FAIL'}")
print(f"  Overall MRR:       {avg_mrr:.4f}  (target={targets.get('mrr', 0.85)})  {'PASS' if avg_mrr >= targets.get('mrr', 0.85) else 'FAIL'}")
print()

for cat in sorted(cat_results):
    cr = cat_results[cat]
    cat_r10 = sum(cr["r10"]) / len(cr["r10"])
    cat_lat = sum(cr["lat"]) / len(cr["lat"])
    print(f"  {cat:20} recall@10={cat_r10:.4f}  avg_lat={cat_lat:.0f}ms")

print(f"\n  queries: {len(test_cases)}")
print(f"  passed:  {sum(1 for r in results['recall_at_10'] if r >= 0.8)}/{len(test_cases)}")
print("=" * 70)
