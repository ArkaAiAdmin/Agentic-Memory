#!/usr/bin/env python3
"""Cron wrapper: re-validate entailment chains.

Scans entailment_chains for chains whose source facts have become
stale (superseded, retracted, or invalidated) and marks them invalid.
This is the periodic sweep that complements the per-supersession
propagation in fact_temporal.py._propagate_entailment_invalidation.

Run from crontab (e.g., hourly):
    0 * * * * .../venv/bin/python .../cron_revalidate_entailments.py >> .../memory/revalidate.log 2>&1

Exit codes:
    0 = OK
    1 = Warning (some chains invalidated)
    2 = Error (DB unavailable or exception)
"""

from _flock import acquire_lock_or_exit
import argparse
import os
import sys
import time
from pathlib import Path

os.chdir(os.path.dirname(os.path.abspath(__file__)))
_parent = os.path.dirname(os.path.abspath(__file__))
if _parent not in sys.path:
    sys.path.insert(0, _parent)


def run(db_path: Path, dry_run: bool = False) -> dict:
    """Run entailment chain revalidation.

    Args:
        db_path: path to memory.db
        dry_run: if True, report what would be invalidated without writing

    Returns:
        {"checked": int, "invalidated": int, "errors": int, "details": list}
    """
    import sqlite3
    from reasoning.compile import revalidate_entailment_chains

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        result = revalidate_entailment_chains(conn, db_path, dry_run=dry_run)
        return result
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-validate entailment chains")
    parser.add_argument("--db", type=str, default=None, help="Path to memory.db")
    parser.add_argument("--dry-run", action="store_true", help="Report only, no writes")
    parser.add_argument("--verbose", action="store_true", help="Detail output")
    args = parser.parse_args()

    from infra.infrastructure import resolve_active_memory_dir
    if args.db:
        db_path = Path(args.db)
    else:
        mem_dir = resolve_active_memory_dir()
        db_path = mem_dir / "memory.db"

    if not acquire_lock_or_exit(str(db_path) + ".revalidate.lock", max_attempts=0):
        print(
            "revalidate_entailments: another instance running, exiting",
            file=sys.stderr,
        )
        return 0

    try:
        t0 = time.time()
        result = run(db_path, dry_run=args.dry_run)
        elapsed_ms = (time.time() - t0) * 1000
        print(
            f"revalidate_entailments: checked={result['checked']} "
            f"invalidated={result['invalidated']} errors={result['errors']} "
            f"in {elapsed_ms:.0f}ms"
        )
        if args.verbose and result.get("invalidated", 0) > 0:
            for item in result.get("details", []):
                print(
                    f"  chain_id={item['chain_id']} derived={item['derived_fact_id']} "
                    f"invalid_source={item['invalid_source_id']}"
                )
        return 1 if result.get("invalidated", 0) > 0 else 0
    except Exception as e:
        print(f"revalidate_entailments: ERROR: {e}", file=sys.stderr)
        return 2
    finally:
        try:
            os.remove(str(db_path) + ".revalidate.lock")
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
