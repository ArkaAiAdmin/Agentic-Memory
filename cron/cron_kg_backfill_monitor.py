#!/usr/bin/env python3
"""Monitor the weekly KG backfill cron.

Reads memory/kg-backfill-cron.log and alerts on:
  - exit_code != 0 in any of the last N runs
  - Large entity/edge drops (suggests data loss, threshold: 10% drop
    in a single run)
  - Cron hasn't run in the expected window (Sunday 03:30, ±15 min)

Usage:
    venv/bin/python cron_kg_backfill_monitor.py
    venv/bin/python cron_kg_backfill_monitor.py --days 7     # check last 7 days
    venv/bin/python cron_kg_backfill_monitor.py --verbose    # show all entries

Exit codes:
    0 = OK
    1 = Warning (large drop or no recent runs)
    2 = Error (cron failed or exit code != 0)
"""

from __future__ import annotations

from _flock import acquire_lock_or_exit
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys
import os
_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)
from memory_config import install_root

REPO = install_root()
LOG_FILE = REPO / "memory" / "kg-backfill-cron.log"

# Cron schedule: Sunday 03:30 (server local time)
CRON_WEEKDAY = 6  # Monday=0, Sunday=6 in Python
CRON_HOUR = 3
CRON_MINUTE = 30
TOLERANCE_MINUTES = 30  # ±30 min for "expected" window

# Threshold for "large drop" warning (fraction of pre count)
LARGE_DROP_THRESHOLD = 0.10


def _load_entries(days: int) -> list[dict]:
    if not LOG_FILE.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    entries: list[dict] = []
    with open(LOG_FILE) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                ts = datetime.fromisoformat(entry["captured_at"])
            except (KeyError, ValueError):
                continue
            if ts >= cutoff:
                entries.append(entry)
    return entries


def _is_in_expected_window(ts: datetime) -> bool:
    """True iff ts is within ±TOLERANCE_MINUTES of the expected cron slot."""
    if ts.weekday() != CRON_WEEKDAY:
        return False
    expected_minute = CRON_HOUR * 60 + CRON_MINUTE
    actual_minute = ts.hour * 60 + ts.minute
    return abs(actual_minute - expected_minute) <= TOLERANCE_MINUTES


def check(entries: list[dict], verbose: bool = False) -> tuple[int, list[str]]:
    """Check entries; return (exit_code, list_of_alerts)."""
    alerts: list[str] = []
    exit_code = 0

    if not entries:
        alerts.append("ERROR: no log entries found — cron has never run")
        return 2, alerts

    # Drop the dry-run entries (they're health checks, not real runs)
    real_runs = [e for e in entries if not e.get("dry_run", False)]
    if verbose:
        print(
            f"Total entries: {len(entries)} "
            f"({len(real_runs)} real, {len(entries) - len(real_runs)} dry-run)"
        )

    # Check each real run
    for e in real_runs:
        rc = e.get("result", {}).get("exit_code", -1)
        if rc != 0:
            alerts.append(f"ERROR: run at {e['captured_at']} exited with code {rc}")
            exit_code = max(exit_code, 2)
            continue

        # Check for large drops
        deltas = e.get("post", {}).get("deltas", {})
        pre = e.get("pre", {})
        for table in ("kg_entities", "kg_edges", "kg_facts"):
            d = deltas.get(table, 0)
            base = pre.get(table, 0)
            if base > 0 and d < 0:
                frac = -d / base
                if frac >= LARGE_DROP_THRESHOLD:
                    alerts.append(
                        f"WARN: {e['captured_at']} dropped {d} {table} "
                        f"({frac * 100:.1f}% of {base})"
                    )
                    exit_code = max(exit_code, 1)

    # Check freshness — most recent entry should be in expected window
    latest = max(entries, key=lambda e: e["captured_at"])
    try:
        latest_ts = datetime.fromisoformat(latest["captured_at"])
    except (KeyError, ValueError):
        alerts.append("ERROR: latest entry has invalid timestamp")
        return max(exit_code, 2), alerts

    if verbose:
        print(
            f"Latest entry: {latest_ts.isoformat()} "
            f"(elapsed {(datetime.now(timezone.utc) - latest_ts).total_seconds():.0f}s ago)"
        )

    if not _is_in_expected_window(latest_ts):
        age_minutes = (datetime.now(timezone.utc) - latest_ts).total_seconds() / 60
        if age_minutes > 60 * 24 * 7:  # > 7 days old
            alerts.append(
                f"ERROR: latest entry is {age_minutes / 60 / 24:.1f} days old — "
                f"cron hasn't run successfully in over a week"
            )
            exit_code = max(exit_code, 2)
        elif age_minutes > 60 * 24 * 2:  # > 2 days old
            alerts.append(
                f"WARN: latest entry is {age_minutes / 60 / 24:.1f} days old — "
                f"expected within 7 days (weekly Sunday 03:30)"
            )
            exit_code = max(exit_code, 1)

    if exit_code == 0 and verbose:
        alerts.append("OK: all checks passed")

    return exit_code, alerts


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor weekly KG backfill cron")
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Check entries from the last N days (default: 7)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show all log entries",
    )
    args = parser.parse_args()
    acquire_lock_or_exit('cron_kg_backfill_monitor')

    entries = _load_entries(args.days)
    exit_code, alerts = check(entries, verbose=args.verbose)

    for a in alerts:
        print(a)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
