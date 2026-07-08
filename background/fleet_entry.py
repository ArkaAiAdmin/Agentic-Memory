"""Standalone entry point for the multi-process reconciler fleet.

Thin wrapper that delegates to ``multiwriter_reconciliation_pool``.  The
pool itself spawns each worker as an independent subprocess
(``background.fleet_worker``), which avoids macOS fork-safety issues
with daemon threads.  This module is the ``-m`` target used by the test
harness and the production reconciler.

Usage:
    python -m background.fleet_entry <journal_path> <target_base_dir> <n_workers> <idle_quit_secs>
"""
from __future__ import annotations

import sys
from pathlib import Path


def run(journal_path: str, target_base_dir: str, n_workers: int = 2, idle_quit_after_secs: float = 5.0) -> int:
    """Entry point — run the fleet supervisor pool."""
    from background.background_worker import multiwriter_reconciliation_pool

    jp = Path(journal_path).resolve()
    tb = Path(target_base_dir).resolve()

    multiwriter_reconciliation_pool(jp, tb, n_workers=n_workers, idle_quit_after_secs=idle_quit_after_secs)
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(f"Usage: {sys.argv[0]} <journal_path> <target_base> <n_workers> [idle_quit_secs]", file=sys.stderr)
        sys.exit(2)
    journal_path = sys.argv[1]
    target_base = sys.argv[2]
    n_workers = int(sys.argv[3])
    idle_quit_secs = float(sys.argv[4]) if len(sys.argv) >= 5 else 5.0
    sys.exit(run(journal_path, target_base, n_workers, idle_quit_secs))