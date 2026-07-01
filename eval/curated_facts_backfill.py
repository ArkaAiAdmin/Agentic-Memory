"""Backfill facts for the curated subset only (Phase 5b).

Scoped to: lessons/, decisions/, projects/, preferences/
Skips: sessions/auto-*, sessions/audit-*, sessions/compaction-*,
       sessions/idle-*, sessions/end-* (via _should_skip_category)

Takes a snapshot of kg_facts before, runs the backfill, then
reports the delta. Does NOT touch other tables.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

INSTALL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_ROOT))
os.chdir(INSTALL_ROOT)

from fact import (  # noqa: E402
    index_facts_for_memory,
    ensure_facts_schema,
)

DB_PATH = INSTALL_ROOT / "memory" / "memory.db"
OUT_PATH = INSTALL_ROOT / "memory" / "backfill_phase5b_results.json"

CURATED_CATEGORIES = ("lessons/", "decisions/", "projects/", "preferences/")
PROGRESS_EVERY = 25  # print progress every N memories


def main() -> int:
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return 1

    import sqlite3

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        ensure_facts_schema(conn)

        # Snapshot: count facts per memory before
        print("=== Phase 5b: curated-subset backfill ===")
        print(f"DB: {DB_PATH}")
        print(f"Categories: {list(CURATED_CATEGORIES)}")
        print()

        # Count current facts (before)
        before_total = conn.execute("SELECT COUNT(*) FROM kg_facts").fetchone()[0]
        before_curated = conn.execute(
            f"SELECT COUNT(*) FROM kg_facts WHERE "
            f"{' OR '.join(['source_memory LIKE ?'] * len(CURATED_CATEGORIES))}",
            [c + "%" for c in CURATED_CATEGORIES],
        ).fetchone()[0]
        print(
            f"Before: {before_total} total facts, {before_curated} from curated categories"
        )
        print()

        # Load curated, non-deleted memories
        placeholders = " OR ".join(["id LIKE ?"] * len(CURATED_CATEGORIES))
        params = [c + "%" for c in CURATED_CATEGORIES]
        rows = conn.execute(
            f"SELECT id, content FROM memories "
            f"WHERE deleted_at IS NULL AND ({placeholders})",
            params,
        ).fetchall()
        print(f"Found {len(rows)} curated memories to process")
        print()

        # Track per-memory delta
        per_mem_before: dict[str, int] = {}
        for r in conn.execute(
            f"SELECT source_memory, COUNT(*) FROM kg_facts "
            f"WHERE {' OR '.join(['source_memory LIKE ?'] * len(CURATED_CATEGORIES))} "
            f"GROUP BY source_memory",
            [c + "%" for c in CURATED_CATEGORIES],
        ).fetchall():
            per_mem_before[r[0]] = r[1]

        # Run extraction + index
        t0 = time.perf_counter()
        processed = 0
        new_facts = 0
        errors = 0
        skipped = 0
        for i, r in enumerate(rows, 1):
            mid = r["id"]
            content = r["content"]
            if not content or len(content) < 20:
                skipped += 1
                continue
            try:
                result = index_facts_for_memory(conn, mid, content)
                new_facts += result.get("facts", 0)
            except Exception as e:
                errors += 1
                print(f"  ERROR {mid}: {e}", flush=True)
                continue
            processed += 1
            if i % PROGRESS_EVERY == 0:
                elapsed_so_far = time.perf_counter() - t0
                rate = processed / elapsed_so_far if elapsed_so_far > 0 else 0
                eta = (len(rows) - i) / rate if rate > 0 else 0
                print(
                    f"  [{i}/{len(rows)}] {processed} processed, {new_facts} facts, "
                    f"{rate:.0f} mem/s, ETA {eta:.0f}s",
                    flush=True,
                )
        conn.commit()
        elapsed = time.perf_counter() - t0

        # Snapshot after
        after_total = conn.execute("SELECT COUNT(*) FROM kg_facts").fetchone()[0]
        after_curated = conn.execute(
            f"SELECT COUNT(*) FROM kg_facts WHERE "
            f"{' OR '.join(['source_memory LIKE ?'] * len(CURATED_CATEGORIES))}",
            [c + "%" for c in CURATED_CATEGORIES],
        ).fetchone()[0]

        # Sample 20 new facts for quality spot-check
        sample_rows = conn.execute(
            f"SELECT subject, predicate, object, confidence, source_memory "
            f"FROM kg_facts "
            f"WHERE {' OR '.join(['source_memory LIKE ?'] * len(CURATED_CATEGORIES))} "
            f"ORDER BY RANDOM() LIMIT 20",
            [c + "%" for c in CURATED_CATEGORIES],
        ).fetchall()

        summary = {
            "phase": "5b",
            "scope": list(CURATED_CATEGORIES),
            "before": {
                "total_facts": before_total,
                "curated_facts": before_curated,
            },
            "after": {
                "total_facts": after_total,
                "curated_facts": after_curated,
            },
            "delta": {
                "total_facts": after_total - before_total,
                "curated_facts": after_curated - before_curated,
            },
            "processed": processed,
            "skipped_short_content": skipped,
            "errors": errors,
            "elapsed_seconds": round(elapsed, 3),
            "mem_per_sec": round(processed / elapsed, 1) if elapsed > 0 else None,
            "sample_facts": [
                {
                    "subject": r["subject"],
                    "predicate": r["predicate"],
                    "object": r["object"],
                    "confidence": r["confidence"],
                    "source_memory": r["source_memory"],
                }
                for r in sample_rows
            ],
        }
        with open(OUT_PATH, "w") as f:
            json.dump(summary, f, indent=2)
        print(f"Wrote {OUT_PATH}")
        print()
        print("=== Summary ===")
        print(f"  Before:  {before_total} total / {before_curated} curated")
        print(f"  After:   {after_total} total / {after_curated} curated")
        print(
            f"  Delta:   {after_total - before_total:+d} total / {after_curated - before_curated:+d} curated"
        )
        print(f"  Processed: {processed} memories ({skipped} skipped, {errors} errors)")
        print(
            f"  Elapsed: {elapsed:.2f}s ({processed / elapsed if elapsed else 0:.0f} mem/s)"
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
