"""Single shard worker for the multi-process reconciler fleet.

This is a minimal entry point that runs one shard of the reconciler
without importing the full background worker initialization that starts
daemon threads at module level.  This avoids macOS fork-safety issues
when spawning worker processes.

The worker imports only the function it needs and runs it directly.
"""
from __future__ import annotations

import sys
from pathlib import Path


def run_worker(
    journal_path: Path,
    target_base: Path,
    worker_id: int,
    n_workers: int,
) -> None:
    """Run one shard of the reconciler loop."""
    # Import the worker function locally to avoid importing
    # background_worker module-level code that starts daemon threads
    # at import time in the parent.
    from background.background_worker import _reconciliation_loop_sharded

    _reconciliation_loop_sharded(journal_path, target_base, worker_id, n_workers)


def main() -> int:
    if len(sys.argv) != 5:
        print(
            f"Usage: {sys.argv[0]} <journal_path> <target_base> <worker_id> <n_workers>",
            file=sys.stderr,
        )
        return 2

    journal_path = Path(sys.argv[1]).resolve()
    target_base = Path(sys.argv[2]).resolve()
    worker_id = int(sys.argv[3])
    n_workers = int(sys.argv[4])

    run_worker(journal_path, target_base, worker_id, n_workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())