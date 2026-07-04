#!/usr/bin/env python3
"""Cron wrapper: integrity check — DB health, FTS consistency, orphan detection."""

from _flock import acquire_lock_or_exit
import os
import sys
from pathlib import Path

os.environ.setdefault("MEMORY_KNOWLEDGE_GRAPH", "1")
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import os

_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)
from infra.infrastructure import resolve_active_memory_dir
from memory_integrity import check_index_integrity, repair_kg_orphans


def main() -> int:
    # argparse handles --help and exits cleanly. The pipeline itself
    # takes no flags.
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("usage: %s [-h|--help]" % sys.argv[0], file=sys.stderr)
        print(
            "Cron job — runs the scheduled operation; no flags required.",
            file=sys.stderr,
        )
        sys.exit(0)

    env = os.environ.get("MEMORY_DB_PATH")
    db_path = Path(env) if env else resolve_active_memory_dir() / "memory.db"
    if not db_path.exists():
        print(f"ERROR: no memory.db at {db_path}")
        sys.exit(1)
    acquire_lock_or_exit("cron_integrity_check")
    report = check_index_integrity(db_path, deep=False)
    print(f"Integrity: {report['summary']}")
    for f in report["findings"][:10]:
        print(
            f"  [{f['severity']}] {f.get('check', f.get('code', '?'))}: {f['message']}"
        )
    if len(report["findings"]) > 10:
        print(f"  ... and {len(report['findings']) - 10} more")
    if not report["findings"]:
        print("  No issues found.")
    # Self-healing: repair orphan KG edges, entities, and backlinks.
    repair_result = repair_kg_orphans(db_path)
    if repair_result["was_orphaned"]:
        print(
            f"Repaired: kg_edges={repair_result['deleted_kg_edges']}, "
            f"kg_entities={repair_result['deleted_kg_entities']}, "
            f"backlinks={repair_result['deleted_backlinks']}"
        )
    return 0


if __name__ == "__main__":
    main()
