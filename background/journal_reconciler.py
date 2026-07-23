#!/usr/bin/env python3
"""Standalone journal reconciler daemon.

Extracted from the inline thread in memory_mcp.py to eliminate
in-process competition between the journal reconciler and MCP tool
calls for the single SQLiteWriteQueue thread.

Runs as a separate process with its own flock, draining the CQRS
write-journal in batches.  Can be invoked:

* As a one-shot drain: ``python -m background.journal_reconciler --drain``
* As a cron task (every 5 min): same command with ``--max-entries=50``
* As a persistent daemon: ``python -m background.journal_reconciler``
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Path bootstrap ────────────────────────────────────────────────────
_THIS = Path(__file__).resolve().parent
_REPO_ROOT = _THIS.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _resolve_paths() -> tuple[Path, Path]:
    """Return (target_base, journal_path)."""
    env_path = os.environ.get("MEMORY_DB_PATH", "").strip()
    if env_path:
        target_base = Path(env_path).parent
    else:
        from infra.infrastructure import resolve_active_memory_dir
        target_base = resolve_active_memory_dir()
    journal_path = target_base / "journal.db"
    return target_base, journal_path


def _drain_once(target_base: Path, journal_path: Path, batch_size: int) -> int:
    """Process one batch of pending journal entries.  Returns count processed."""
    from infra.write_journal import dequeue_pending, reset_stuck_processing

    if not journal_path.exists():
        return 0

    reset_stuck_processing(journal_path)
    entries = dequeue_pending(journal_path, batch_size=batch_size)
    if not entries:
        return 0

    from save.pipeline import materialize_journal_entry

    processed = 0
    for entry in entries:
        try:
            materialize_journal_entry(entry, target_base, journal_path)
            processed += 1
        except Exception as exc:
            logger.exception(
                "reconciler: entry %s failed: %s",
                entry.get("note_id", "?"),
                exc,
            )
    return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="Journal reconciler")
    parser.add_argument(
        "--drain", action="store_true",
        help="Run in one-shot drain mode (process pending and exit).",
    )
    parser.add_argument(
        "--max-entries", type=int, default=50,
        help="Maximum entries to process in drain mode (default 50).",
    )
    parser.add_argument(
        "--idle-seconds", type=float, default=5.0,
        help="Idle sleep between drain batches in daemon mode (default 5).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Flock guard — only one reconciler process at a time.
    try:
        from cron._flock import acquire_lock_or_exit
        acquire_lock_or_exit("journal_reconciler")
    except SystemExit:
        return
    except Exception as exc:
        logger.warning("flock guard skipped: %s", exc)

    target_base, journal_path = _resolve_paths()

    # Graceful shutdown on SIGTERM/SIGINT.
    _shutdown = False

    def _handle_signal(signum: int, _frame: object) -> None:
        nonlocal _shutdown
        _shutdown = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    if args.drain:
        # One-shot: process up to --max-entries and exit.
        remaining = args.max_entries
        total = 0
        while remaining > 0 and not _shutdown:
            batch = min(remaining, 10)
            n = _drain_once(target_base, journal_path, batch)
            total += n
            if n == 0:
                break
            remaining -= n
        logger.info("journal_reconciler --drain: processed %d entries", total)
        return

    # Persistent daemon mode.
    logger.info(
        "journal_reconciler: daemon started (journal=%s)", journal_path
    )
    while not _shutdown:
        try:
            n = _drain_once(target_base, journal_path, batch_size=10)
            if n == 0:
                time.sleep(args.idle_seconds)
        except Exception as loop_exc:
            logger.error("reconciler loop error: %s", loop_exc)
            time.sleep(1.0)
    logger.info("journal_reconciler: shutdown complete")


if __name__ == "__main__":
    main()
