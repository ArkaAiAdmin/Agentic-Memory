#!/usr/bin/env python3
from __future__ import annotations
"""Cron wrapper: embedding_recompute — detect model change, auto-rebuild vec index.

Compares the current embedding model config against the stored vec
index metadata. If the model has changed (dimensions, model name, or
api_base), triggers a full vec index rebuild.

Usage:
    venv/bin/python cron_embedding_recompute.py --once
    venv/bin/python cron_embedding_recompute.py --once --force
    venv/bin/python cron_embedding_recompute.py --once --dry-run

Run from crontab (daily 04:00):
    0 4 * * * .../venv/bin/python .../cron_embedding_recompute.py --once >> .../memory/embedding-recompute.log 2>&1
"""

from _flock import acquire_lock_or_exit
import argparse
import os
import sys
import time
import traceback
from pathlib import Path

# Anchor at the package root so imports work regardless of cwd.
_PACKAGE_ROOT = Path(__file__).resolve().parent
if _PACKAGE_ROOT.name == "cron":
    _PACKAGE_ROOT = _PACKAGE_ROOT.parent
sys.path.insert(0, str(_PACKAGE_ROOT))
os.chdir(str(_PACKAGE_ROOT))

from infra.memory_common import GLOBAL_MEM_DIR
from infra.log import setup_logging

setup_logging(__name__)


def _run_check_and_rebuild(args: argparse.Namespace) -> int:
    db_path = GLOBAL_MEM_DIR / "memory.db"
    if not db_path.exists():
        print(f"ERROR: no memory.db at {db_path}")
        return 1
    t0 = time.time()
    try:
        from infra.embedding_recompute import check_and_rebuild

        stats = check_and_rebuild(force=args.force, dry_run=args.dry_run)
        elapsed = time.time() - t0
        prefix = "[DRY RUN] " if args.dry_run else ""
        if stats.get("changed") and stats.get("rebuilt"):
            print(
                f"{prefix}Embedding recomputation: {stats['details']} ({elapsed:.2f}s)"
            )
        elif stats.get("changed"):
            print(
                f"{prefix}Embedding recomputation: {stats['details']} ({elapsed:.2f}s)"
            )
        else:
            print(f"Embedding recomputation: {stats['details']} ({elapsed:.2f}s)")
        return 0
    except Exception:
        print("ERROR: embedding_recompute failed with exception:")
        traceback.print_exc()
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect embedding model change, auto-rebuild vec index."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=True,
        help="Run a single check + rebuild cycle and exit (default).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force a rebuild regardless of whether the model has changed.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes.",
    )
    args = parser.parse_args(argv)
    acquire_lock_or_exit('cron_embedding_recompute')

    from background.cron_model_lock import cron_model_lock
    with cron_model_lock("embedding_recompute", timeout=600.0):
        return _run_check_and_rebuild(args)


if __name__ == "__main__":
    sys.exit(main())
