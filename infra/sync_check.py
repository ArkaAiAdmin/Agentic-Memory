#!/usr/bin/env python3
"""sync_check — CLI tool to verify subsystem health.

Prints a table showing each subsystem's row count and health status.
Exits with code 1 if any subsystem is in drift or empty state.

Usage:
    python sync_check.py              # local DB
    python sync_check.py --global     # global DB
    python sync_check.py --json       # JSON output
"""
import argparse
import json
import sys
from pathlib import Path

# Ensure we can import from this directory
import os
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory_common import get_memory_paths, connection_pool, safe_close_db
from sync_invariant import check_sync_invariant, format_sync_report, get_drifted_subsystems


def main(db_path: str | None = None):
    parser = argparse.ArgumentParser(description="Check memory subsystem sync health")
    parser.add_argument("--global", dest="use_global", action="store_true",
                        help="Check the global DB instead of local")
    parser.add_argument("--json", dest="json_output", action="store_true",
                        help="Output results as JSON")
    args = parser.parse_args()

    if db_path is None:
        db_path = os.environ.get("MEMORY_DB_PATH")
    if db_path is None:
        global_mem, local_mem, _ = get_memory_paths()
        db_path = str((global_mem if args.use_global else local_mem) / "memory.db")

    conn = connection_pool.get(db_path, timeout=10.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    try:
        result = check_sync_invariant(conn)
    finally:
        safe_close_db(conn)

    if args.json_output:
        print(json.dumps(result, indent=2))
    else:
        label = "GLOBAL" if args.use_global else "LOCAL"
        print(f"Database: {label} ({db_path})")
        print()
        print(format_sync_report(result))

    drifted = get_drifted_subsystems(result)
    if drifted:
        if not args.json_output:
            print(f"\nDrifted: {', '.join(drifted)}")
        sys.exit(1)
    else:
        if not args.json_output:
            print("\nAll subsystems healthy.")
        sys.exit(0)


if __name__ == "__main__":
    main()
