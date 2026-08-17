import json
from collections import defaultdict

with open('eval/results/latest_longmemeval_s.json') as f:
    data = json.load(f)

results = data.get('results', [])
fails_by_cat = defaultdict(list)

for r in results:
    if r.get('scores', {}).get('overall_accuracy', 0.0) < 0.5:
        fails_by_cat[r.get('category')].append(r)

print(f"Total Evaluated: {len(results)} | Total Failures: {sum(len(v) for v in fails_by_cat.values())}")
for cat, fails in sorted(fails_by_cat.items()):
    print(f"\n=======================================================")
    print(f"CATEGORY: {cat} ({len(fails)} failures)")
    print(f"=======================================================")
    for idx, r in enumerate(fails, start=1):
        qid = r.get('question_id')
        q = r.get('query')
        exp = str(r.get('expected') or '')[:120].replace('\n', ' ')
        print(f"{idx:2d}. [{qid}] {q}")
        print(f"    -> Expected: {exp}")
