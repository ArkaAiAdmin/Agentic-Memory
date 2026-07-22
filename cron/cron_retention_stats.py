#!/usr/bin/env python3
"""Cron wrapper: adaptive retention + neural forget curve."""

from _flock import acquire_lock_or_exit
import os
import sys
import traceback

os.environ.setdefault("MEMORY_ADAPTIVE_RETENTION", "1")
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_repo_root)
import os

_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)
import adaptive_retention as ar
import neural_forget as nf


def main() -> int:
    # argparse handles --help and exits cleanly. The pipeline itself
    # takes no flags.
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("usage: %s [-h|--help]" % sys.argv[0], file=sys.stderr)
        print("Cron job — runs the scheduled operation; no flags required.", file=sys.stderr)
        sys.exit(0)

    acquire_lock_or_exit("cron_retention_stats")
    try:
        if ar.ADAPTIVE_RETENTION_ENABLED:
            result = ar.batch_update_retention(dry_run=False)
            print(
                f"Adaptive retention: updated={result.get('updated', 0)}, "
                f"skipped={result.get('skipped', 0)}"
            )
        else:
            print("MEMORY_ADAPTIVE_RETENTION not enabled, skipping adaptive retention.")

        from infra.infrastructure import resolve_active_memory_dir

        db_path = resolve_active_memory_dir() / "memory.db"
        if db_path.exists():
            nf_result = nf.batch_update_retention(db_path)
            print(
                f"Neural forget curve: updated={nf_result.get('updated', 0)}, "
                f"failed={nf_result.get('failed', 0)}"
            )
    except Exception as e:
        print(f"cron_retention_stats FAILED: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
    return 0


if __name__ == "__main__":
    main()
