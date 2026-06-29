#!/usr/bin/env python3
"""Cron wrapper: hard-delete soft-deleted notes older than 30 days."""

from _flock import acquire_lock_or_exit
import os
import sys
import traceback
from pathlib import Path


_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)
from memory_common import get_memory_paths
from memory_delete import purge_expired


def main(db_path: str | None = None) -> int:
    # argparse handles --help and exits cleanly. The pipeline itself
    # takes no flags.
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("usage: %s [-h|--help]" % sys.argv[0], file=sys.stderr)
        print("Cron job — runs the scheduled operation; no flags required.", file=sys.stderr)
        sys.exit(0)

    acquire_lock_or_exit("cron_purge_expired")
    try:
        if db_path is None:
            db_path = os.environ.get("MEMORY_DB_PATH")
        if db_path is not None:
            resolved = Path(db_path)
        else:
            _, local_mem, _ = get_memory_paths()
            resolved = local_mem / "memory.db"

        if resolved.exists():
            n = purge_expired(resolved)
            print(f"Purged {n} expired note(s).")
        else:
            print("No memory.db found.")
    except Exception as e:
        print(f"cron_purge_expired FAILED: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
