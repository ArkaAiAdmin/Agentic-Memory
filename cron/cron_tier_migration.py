#!/usr/bin/env python3
"""Cron wrapper: tier migration — consolidate warm sessions, archive cold files.

Hot tier:  <7 days, full-content files, indexed at full resolution.
Warm tier: 7-90 days, session logs consolidated into lessons/ summaries.
Cold tier: >90 days, archived to gzip bundles, replaced with stubs.

Run from crontab (weekly Sunday 03:00):
    0 3 * * 0 .../venv/bin/python .../cron_tier_migration.py --once >> .../memory/tier-migration.log 2>&1

Or invoke directly:
    venv/bin/python cron_tier_migration.py --once
    venv/bin/python cron_tier_migration.py --once --dry-run
"""

from _flock import acquire_lock_or_exit
import argparse
import os
import sys
import time
import traceback
from pathlib import Path

os.environ.setdefault("MEMORY_TEMPORAL_TIERS", "1")

# Anchor at the package root so imports work regardless of cwd.
_PACKAGE_ROOT = Path(__file__).resolve().parent
if _PACKAGE_ROOT.name == "cron":
    _PACKAGE_ROOT = _PACKAGE_ROOT.parent
sys.path.insert(0, str(_PACKAGE_ROOT))
os.chdir(str(_PACKAGE_ROOT))

from memory_common import configure_logging, GLOBAL_MEM_DIR
from infrastructure import resolve_active_memory_dir

configure_logging()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Tier migration lifecycle (consolidate warm, archive cold, prune superseded)."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=True,
        help="Run a single migration cycle and exit (default).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes.",
    )
    parser.add_argument(
        "--memory-dir",
        default=None,
        help=f"Memory directory (default: {GLOBAL_MEM_DIR})",
    )
    args = parser.parse_args(argv)
    acquire_lock_or_exit('cron_tier_migration')

    memory_dir = (
        Path(args.memory_dir) if args.memory_dir else resolve_active_memory_dir()
    )
    if not memory_dir.exists():
        print(f"ERROR: memory dir {memory_dir} does not exist")
        return 1

    t0 = time.time()
    print(f"Starting tier migration on {memory_dir} (dry_run={args.dry_run})")

    try:
        from tier_migration import (
            consolidate_warm_sessions,
            archive_cold_files,
            prune_superseded,
        )

        # Warm tier: consolidate session logs (7-90 days)
        consolidate_warm_sessions(memory_dir, dry_run=args.dry_run)

        # Cold tier: archive >90 days
        cold_stats = archive_cold_files(memory_dir, dry_run=args.dry_run)

        # Prune superseded notes to bundles
        prune_stats = prune_superseded(memory_dir, dry_run=args.dry_run)

        elapsed = time.time() - t0
        prefix = "[DRY RUN] " if args.dry_run else ""
        print(
            f"{prefix}Tier migration complete in {elapsed:.2f}s\n"
            f"  cold: archived={cold_stats.get('archived', 0)} "
            f"skipped={cold_stats.get('skipped', 0)} "
            f"pinned_protected={cold_stats.get('pinned_protected', 0)}\n"
            f"  prune: pruned={prune_stats.get('pruned', 0)} "
            f"skipped={prune_stats.get('skipped', 0)}"
        )
        return 0
    except Exception:
        print("ERROR: tier migration failed with exception:")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
