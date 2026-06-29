"""Post-Layer-5 re-measurement for fact extraction on curated subset.

Same setup as curated_facts_baseline.py but with the new patterns
enabled. The category filter and the 4 broader patterns (Layer 5) are
now active. Compares results to the baseline JSON to compute the delta.

Reads from the live DB, runs extract_facts() in-process, writes a JSON
review file. Does NOT write to the DB. Safe to re-run.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
import time
from pathlib import Path

INSTALL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_ROOT))
os.chdir(INSTALL_ROOT)

from fact_extraction import extract_facts, _is_valid  # noqa: E402

DB_PATH = INSTALL_ROOT / "memory" / "memory.db"
OUT_PATH = INSTALL_ROOT / "memory" / "curated_facts_after.json"
BASELINE_PATH = INSTALL_ROOT / "memory" / "curated_facts_baseline.json"

CATEGORIES = ("lessons/", "decisions/", "projects/", "preferences/")
SAMPLE_SIZE = 200
SEED = 42  # same seed as baseline for direct comparability


def load_curated(conn: sqlite3.Connection, limit: int) -> list[tuple[str, str]]:
    placeholders = " OR ".join(["id LIKE ?"] * len(CATEGORIES))
    params = [c + "%" for c in CATEGORIES] + [limit]
    rows = conn.execute(
        f"SELECT id, content FROM memories "
        f"WHERE deleted_at IS NULL AND ({placeholders}) "
        f"ORDER BY RANDOM() LIMIT ?",
        params,
    ).fetchall()
    return rows


def main() -> int:
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return 1

    print("=== After: fact extraction on curated subset (Layer 5 active) ===")
    print(f"DB: {DB_PATH}")
    print(f"Categories: {CATEGORIES}")
    print(f"Sample size: {SAMPLE_SIZE}, seed: {SEED}")

    rng = random.Random(SEED)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    try:
        all_rows = load_curated(conn, limit=SAMPLE_SIZE)
        rng.shuffle(all_rows)
        rows = all_rows[:SAMPLE_SIZE]
    finally:
        conn.close()

    print(f"Loaded {len(rows)} curated memories")
    t0 = time.perf_counter()
    results = []
    no_match = 0
    category_counts: dict[str, int] = {}
    pred_counts: dict[str, int] = {}
    for mid, content in rows:
        category = mid.split("/", 1)[0] if "/" in mid else "other"
        category_counts[category] = category_counts.get(category, 0) + 1
        try:
            facts = extract_facts(content)
        except Exception as e:
            facts = [("__ERROR__", "error", repr(e)[:80], 0.0)]
        valid_facts = [
            {
                "memory_id": mid,
                "subject": s,
                "predicate": p,
                "object": o,
                "confidence": c,
            }
            for s, p, o, c in facts
            if p != "error" and _is_valid(s, o)
        ]
        for vf in valid_facts:
            pred_counts[vf["predicate"]] = pred_counts.get(vf["predicate"], 0) + 1
        if not valid_facts:
            no_match += 1
        results.append(
            {
                "memory_id": mid,
                "category": category,
                "content_preview": content[:200].replace("\n", " "),
                "fact_count": len(valid_facts),
                "facts": valid_facts,
            }
        )
    elapsed = time.perf_counter() - t0

    total_facts = sum(r["fact_count"] for r in results)
    summary = {
        "phase": "after",
        "categories": list(CATEGORIES),
        "sample_size": SAMPLE_SIZE,
        "seed": SEED,
        "category_distribution": category_counts,
        "memories_with_facts": SAMPLE_SIZE - no_match,
        "memories_with_no_facts": no_match,
        "no_match_pct": round(100 * no_match / SAMPLE_SIZE, 1),
        "total_facts_extracted": total_facts,
        "avg_facts_per_memory": round(total_facts / SAMPLE_SIZE, 2),
        "predicate_distribution": pred_counts,
        "elapsed_seconds": round(elapsed, 3),
        "mem_per_sec": round(SAMPLE_SIZE / elapsed, 1) if elapsed > 0 else None,
    }
    with open(OUT_PATH, "w") as f:
        json.dump({"summary": summary, "results": results}, f, indent=2)
    print(f"  Wrote {OUT_PATH}")
    print("")
    print("=== Summary ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    # Compute delta vs baseline
    if BASELINE_PATH.exists():
        with open(BASELINE_PATH) as f:
            baseline = json.load(f)["summary"]
        b_facts = baseline["total_facts_extracted"]
        a_facts = total_facts
        delta_facts = a_facts - b_facts
        delta_pct = round(100 * delta_facts / b_facts, 1) if b_facts else 0
        print("\n=== Delta vs baseline ===")
        print(f"  baseline facts:    {b_facts}")
        print(f"  after facts:       {a_facts}")
        print(f"  delta:             {delta_facts:+d} ({delta_pct:+.1f}%)")
        print(
            f"  baseline no-match: {baseline['memories_with_no_facts']} ({baseline['no_match_pct']}%)"
        )
        print(
            f"  after no-match:    {no_match} ({round(100 * no_match / SAMPLE_SIZE, 1)}%)"
        )
        print(f"  baseline mem/sec:  {baseline['mem_per_sec']}")
        print(
            f"  after mem/sec:     {round(SAMPLE_SIZE / elapsed, 1) if elapsed > 0 else None}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
