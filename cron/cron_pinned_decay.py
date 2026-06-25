#!/usr/bin/env python3
"""Cron wrapper: check pinned notes for drift, auto-unpin stale ones."""

from _flock import acquire_lock_or_exit
import os, sys, traceback

os.environ.setdefault("MEMORY_KNOWLEDGE_GRAPH", "1")
import sys
import os
_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)
from pinned_decay import main as pinned_main


def main() -> int:
    # argparse handles --help and exits cleanly. The pipeline itself
    # takes no flags.
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("usage: %s [-h|--help]" % sys.argv[0], file=sys.stderr)
        print("Cron job — runs the scheduled operation; no flags required.", file=sys.stderr)
        sys.exit(0)

    try:
        pinned_main()
    except Exception as e:
        print(f"cron_pinned_decay FAILED: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
    acquire_lock_or_exit('cron_pinned_decay')


if __name__ == "__main__":
    main()
