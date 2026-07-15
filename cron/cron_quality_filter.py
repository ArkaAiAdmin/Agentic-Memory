#!/usr/bin/env python3
"""Cron wrapper: quality gate stats — validation + dedup metrics."""
from _flock import acquire_lock_or_exit
import os
import sys
import json
from pathlib import Path
os.environ.setdefault("MEMORY_QUALITY_GATES", "1")
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import os
_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)
import sqlite3
from infra.infrastructure import resolve_active_memory_dir
import quality_gates as qg

def main() -> int:
    acquire_lock_or_exit('cron_quality_filter')

    # argparse handles --help and exits cleanly. The pipeline itself
    # takes no flags.
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("usage: %s [-h|--help]" % sys.argv[0], file=sys.stderr)
        print("Cron job — runs the scheduled operation; no flags required.", file=sys.stderr)
        sys.exit(0)

    if not qg.QUALITY_GATES_ENABLED:
        print("MEMORY_QUALITY_GATES not enabled, skipping.")
        return 0
    env = os.environ.get("MEMORY_DB_PATH")
    db_path = Path(env) if env else resolve_active_memory_dir() / "memory.db"
    if not db_path.exists():
        print(f"ERROR: no memory.db at {db_path}")
        sys.exit(1)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    try:
        stats = qg.quality_stats(conn)
        print(f"Quality stats: {json.dumps(stats, indent=2)}")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    main()
