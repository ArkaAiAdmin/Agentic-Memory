#!/usr/bin/env python3
"""Cron wrapper: auto-summarize — compress long notes via extractive TF-IDF."""

from _flock import acquire_lock_or_exit
import os
import sys
import traceback

os.environ.setdefault("MEMORY_SUMMARIZATION", "1")
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import os
_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)
import summarization as sm


def main() -> int:
    acquire_lock_or_exit('cron_auto_summarize')
    # argparse handles --help and exits cleanly. The pipeline itself
    # takes no flags.
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("usage: %s [-h|--help]" % sys.argv[0], file=sys.stderr)
        print("Cron job — runs the scheduled operation; no flags required.", file=sys.stderr)
        sys.exit(0)

    try:
        if not sm.SUMMARIZATION_ENABLED:
            print("MEMORY_SUMMARIZATION not enabled, skipping.")
            return 0
        db_path = os.environ.get("MEMORY_DB_PATH")
        if db_path:
            result = sm.auto_summarize_long(min_length=500, dry_run=False, db_path=db_path)
        else:
            result = sm.auto_summarize_long(min_length=500, dry_run=False)
        print(
            f"Auto-summarize: summarized={result.get('summarized', 0)}, "
            f"skipped={result.get('skipped', 0)}"
        )
    except Exception as e:
        print(f"cron_auto_summarize FAILED: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
    return 0


if __name__ == "__main__":
    main()
