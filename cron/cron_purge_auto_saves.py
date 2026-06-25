#!/usr/bin/env python3
"""Cron wrapper: delete all auto-saved tool-log entries from DB and disk."""

from _flock import acquire_lock_or_exit
import os, sys, traceback, json
from pathlib import Path

import sys
import os

_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)
from memory_common import get_memory_paths
from auto_save import purge_auto_saves


def main(db_path: str | None = None) -> int:
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("usage: %s [-h|--help] [--dry-run]" % sys.argv[0], file=sys.stderr)
        print("Cron job — deletes all auto-saved tool-log entries.", file=sys.stderr)
        sys.exit(0)

    dry_run = "--dry-run" in sys.argv[1:]

    acquire_lock_or_exit("cron_purge_auto_saves")
    try:
        result = purge_auto_saves(dry_run=dry_run)
        print(json.dumps(result, indent=2))
        if "error" in result:
            sys.exit(1)
    except Exception as e:
        print(f"cron_purge_auto_saves FAILED: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
