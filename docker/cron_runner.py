"""Containerized cron scheduler for agentic-memory.

Runs each entry from `cron/schedule.json` in a loop, sleeping
until the next due time. This replaces the host crontab so the
cron service is portable across containers.

Supports two schedule.json formats:

    # New format (cron expression + full command)
    {"cmd": "venv/bin/python -m cron.consolidate", "schedule": "0 0 * * 0"}

    # Legacy format (kept for backward compatibility)
    {"name": "cron_consolidate.py", "interval_minutes": 60, "args": [], "enabled": true}

This file is part of Phase 4.1 (Docker compose).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger("agentic_memory.cron_runner")
DEFAULT_SCHEDULE = Path("/app/cron/schedule.json")


def load_schedule(path: Path) -> list[dict]:
    with path.open() as f:
        items = json.load(f)
    if not isinstance(items, list):
        raise ValueError(f"schedule.json must be a list, got {type(items).__name__}")
    return items


def _entry_key(entry: dict) -> str:
    """Stable dedup key so cron-expression and legacy entries both track last-run."""
    return entry.get("cmd") or entry.get("name") or json.dumps(entry, sort_keys=True)


def run_once(entry: dict, scripts_dir: Path) -> None:
    cmd = entry.get("cmd")
    if cmd:
        # New format: full command string relative to the project root.
        parts = shlex.split(cmd)
        script = Path(parts[0])
        if not script.is_absolute() and not script.exists():
            # Resolve relative to scripts_dir (legacy layout) or project root.
            candidate = scripts_dir / parts[0]
            if candidate.exists():
                script = candidate
        if not script.exists():
            logger.error("script not found: %s", script)
            return
        full_cmd = [str(script), *parts[1:]]
    else:
        # Legacy format: name + args resolved under scripts_dir.
        name = entry["name"]
        args = entry.get("args", [])
        script_path = scripts_dir / name
        if not script_path.exists():
            logger.error("script not found: %s", script_path)
            return
        full_cmd = [sys.executable, str(script_path), *args]

    extra_env = entry.get("env", {})
    started = time.time()
    run_env = os.environ.copy()
    run_env.update({k: str(v) for k, v in extra_env.items()})
    logger.info("running %s", full_cmd)
    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=entry.get("timeout_seconds", 300),
            env=run_env,
        )
        elapsed = time.time() - started
        label = _entry_key(entry)
        if result.returncode == 0:
            logger.info("ok %s in %.1fs", label, elapsed)
        else:
            logger.error(
                "fail %s rc=%d in %.1fs — stderr=%s",
                label,
                result.returncode,
                elapsed,
                result.stderr[:500],
            )
    except subprocess.TimeoutExpired:
        logger.error("timeout %s after %ds", _entry_key(entry), entry.get("timeout_seconds", 300))


def _cron_fields(schedule: str) -> list[str]:
    return schedule.strip().split()


def _matches_field(field: str, val: int, max_val: int) -> bool:
    """Match a single cron field (supports *, */n, lists, ranges, exact)."""
    if field == "*":
        return True
    for part in field.split(","):
        if part.startswith("*/"):
            step = int(part[2:])
            if val % step == 0:
                return True
        elif "-" in part:
            lo, hi = part.split("-")
            if int(lo) <= val <= int(hi):
                return True
        elif val == int(part):
            return True
    return False


def _next_cron_due(schedule: str, now_ts: float, last_run: float) -> float:
    """Return the next timestamp (epoch s) at or after now that satisfies the cron expr.

    Uses a 1-minute granularity scan up to 1 year out (safe upper bound).
    """
    minute, hour, dom, month, dow = _cron_fields(schedule)
    t = int(now_ts)
    # Align to the start of the current minute.
    t -= t % 60
    for _ in range(60 * 24 * 366):  # up to ~1 year
        lt = time.localtime(t)
        if (
            _matches_field(minute, lt.tm_min, 59)
            and _matches_field(hour, lt.tm_hour, 23)
            and _matches_field(dom, lt.tm_mday, 31)
            and _matches_field(month, lt.tm_mon, 12)
            and _matches_field(dow, (lt.tm_wday + 1) % 7, 6)
        ):
            # Don't return a time in the past relative to now.
            if t >= int(now_ts):
                return float(t)
        t += 60
    return float(now_ts)


def next_due(entry: dict, now_ts: float, last_run: float) -> float:
    """Return the next due time (epoch seconds) for an entry.

    `last_run` is the timestamp of the last run; 0 means never.
    Supports both cron-expression ("schedule") and legacy ("interval_minutes").
    """
    schedule = entry.get("schedule")
    if schedule:
        # For cron expressions we compute from now, not from last run, so we don't
        # drift when the process restarts. But if a run is overdue (last_run older
        # than the next scheduled slot), fire immediately.
        nxt = _next_cron_due(schedule, now_ts, last_run)
        if last_run == 0 or last_run < nxt - 60:
            return now_ts if last_run == 0 else min(nxt, now_ts)
        return nxt
    interval_s = entry["interval_minutes"] * 60
    if last_run == 0:
        return now_ts
    return float(last_run + interval_s)


def main() -> int:
    parser = argparse.ArgumentParser(description="Containerized cron runner")
    parser.add_argument(
        "--schedule",
        type=Path,
        default=DEFAULT_SCHEDULE,
        help="Path to schedule.json (default: /app/cron/schedule.json)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run all enabled entries once and exit (for smoke tests)",
    )
    parser.add_argument(
        "--scripts-dir",
        type=Path,
        default=Path("/app/cron"),
        help="Directory containing the cron scripts",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    schedule = load_schedule(args.schedule)
    enabled = [e for e in schedule if e.get("enabled", True)]
    logger.info(
        "loaded %d schedule entries (%d enabled)",
        len(schedule),
        len(enabled),
    )

    if args.once:
        for entry in enabled:
            run_once(entry, args.scripts_dir)
        return 0

    last_runs: dict[str, float] = {}
    logger.info("entering cron loop — pid=%d", os.getpid())
    while True:
        now_ts = time.time()
        for entry in enabled:
            key = _entry_key(entry)
            last = last_runs.get(key, 0)
            due = next_due(entry, now_ts, last)
            if due <= now_ts:
                run_once(entry, args.scripts_dir)
                last_runs[key] = time.time()
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
