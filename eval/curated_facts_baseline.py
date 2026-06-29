"""Baseline measurement for fact extraction on curated subset.

Curated subset = knowledge-bearing categories only:
  lessons/, decisions/, projects/, preferences/

Excluded (operational noise):
  sessions/auto-*, sessions/audit-*, sessions/compaction-*,
  sessions/idle-*, sessions/end-*

Reads from the live DB, runs extract_facts() in-process, writes a
JSON review file. Does NOT write to the DB. Safe to re-run.
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
OUT_PATH = INSTALL_ROOT / "memory" / "curated_facts_baseline.json"

CATEGORIES = ("lessons/", "decisions/", "projects/", "preferences/")
SAMPLE_SIZE = 200
SEED = 42  # deterministic so we can compare apples-to-apples


def is_curated(mid: str) -> bool:
    return any(mid.startswith(c) for c in CATEGORIES)


def load_curated(conn: sqlite3.Connection, limit: int) -> list[tuple[str, str]]:
    """Return (memory_id, content) for up to `limit` curated, non-deleted memories."""
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

    print("=== Baseline: fact extraction on curated subset ===")
    print(f"DB: {DB_PATH}")
    print(f"Categories: {CATEGORIES}")
    print(f"Sample size: {SAMPLE_SIZE}, seed: {SEED}")

    rng = random.Random(SEED)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    try:
        all_rows = load_curated(conn, limit=SAMPLE_SIZE)
        # Re-shuffle with our RNG for reproducibility (SQLite's RANDOM is its own RNG)
        rng.shuffle(all_rows)
        # Re-truncate to sample size in case DB returned more
        rows = all_rows[:SAMPLE_SIZE]
    finally:
        conn.close()

    print(f"Loaded {len(rows)} curated memories")
    t0 = time.perf_counter()
    results = []
    no_match = 0
    category_counts: dict[str, int] = {}
    for mid, content in rows:
        # category is everything before the first '/'
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
        "phase": "baseline",
        "categories": list(CATEGORIES),
        "sample_size": SAMPLE_SIZE,
        "seed": SEED,
        "category_distribution": category_counts,
        "memories_with_facts": SAMPLE_SIZE - no_match,
        "memories_with_no_facts": no_match,
        "no_match_pct": round(100 * no_match / SAMPLE_SIZE, 1),
        "total_facts_extracted": total_facts,
        "avg_facts_per_memory": round(total_facts / SAMPLE_SIZE, 2),
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
