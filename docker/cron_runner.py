"""Containerized cron scheduler for agentic-memory.

Runs each entry from `cron/schedule.json` in a loop, sleeping
until the next due time. This replaces the host crontab so the
cron service is portable across containers.

Reads schedule.json:
    {
        "name": "cron_consolidate.py",
        "interval_minutes": 60,
        "args": [],
        "enabled": true
    }

This file is part of Phase 4.1 (Docker compose).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

LOG = logging.getLogger("agentic_memory.cron_runner")
DEFAULT_SCHEDULE = Path("/app/cron/schedule.json")


def load_schedule(path: Path) -> list[dict]:
    with path.open() as f:
        items = json.load(f)
    if not isinstance(items, list):
        raise ValueError(f"schedule.json must be a list, got {type(items).__name__}")
    return items


def run_once(entry: dict, scripts_dir: Path) -> None:
    name = entry["name"]
    args = entry.get("args", [])
    extra_env = entry.get("env", {})
    started = time.time()
    script_path = scripts_dir / name
    if not script_path.exists():
        LOG.error("script not found: %s", script_path)
        return
    cmd = [sys.executable, str(script_path), *args]
    run_env = os.environ.copy()
    run_env.update({k: str(v) for k, v in extra_env.items()})
    LOG.info("running %s (args=%s)", name, args)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=entry.get("timeout_seconds", 300),
            env=run_env,
        )
        elapsed = time.time() - started
        if result.returncode == 0:
            LOG.info("ok %s in %.1fs", name, elapsed)
        else:
            LOG.error(
                "fail %s rc=%d in %.1fs — stderr=%s",
                name,
                result.returncode,
                elapsed,
                result.stderr[:500],
            )
    except subprocess.TimeoutExpired:
        LOG.error("timeout %s after %ds", name, entry.get("timeout_seconds", 300))


def next_due(entry: dict, now_ts: float, last_run: float) -> float:
    """Return the next due time (epoch seconds) for an entry.

    `last_run` is the timestamp of the last run; 0 means never.
    """
    interval_s = entry["interval_minutes"] * 60
    if last_run == 0:
        return now_ts
    return last_run + interval_s


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
    LOG.info(
        "loaded %d schedule entries (%d enabled)",
        len(schedule),
        len(enabled),
    )

    if args.once:
        for entry in enabled:
            run_once(entry, args.scripts_dir)
        return 0

    last_runs: dict[str, float] = {}
    LOG.info("entering cron loop — pid=%d", os.getpid())
    while True:
        now_ts = time.time()
        for entry in enabled:
            name = entry["name"]
            last = last_runs.get(name, 0)
            due = next_due(entry, now_ts, last)
            if due <= now_ts:
                run_once(entry, args.scripts_dir)
                last_runs[name] = time.time()
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
